#!/usr/bin/env python3
"""
Weekly calls tracker.

Monday after the open : records entry prices for the week's calls, posts them.
Friday before the close: records exits, scores each call against the benchmark,
                         posts a scorecard, appends to calls_results.csv.

Reads calls.yaml. Reuses watcher.py's Discord plumbing. Cannot trade.

The value of this script is that git timestamps the calls. A pick committed
Sunday night cannot be quietly rewritten on Friday.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from watcher import DISCORD_WEBHOOK, HTTP_TIMEOUT, iso, now_utc, sanitize

ROOT = Path(__file__).parent
CALLS_PATH = ROOT / "calls.yaml"
STATE_PATH = ROOT / "calls_state.json"
RESULTS_PATH = ROOT / "calls_results.csv"

ET = ZoneInfo("America/New_York")
RESULT_FIELDS = [
    "week_of", "ticker", "author", "direction", "confidence",
    "entry", "exit", "pct", "benchmark_pct", "vs_benchmark", "correct", "reasoning",
]


def price(symbol: str) -> float | None:
    try:
        import yfinance as yf

        last = float(yf.Ticker(symbol).fast_info["last_price"])
        return last if last > 0 else None
    except Exception as exc:  # noqa: BLE001
        print(f"  ! price failed for {symbol}: {exc}")
        return None


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def post(lines: list[str]) -> None:
    body = "\n".join(lines)[:1900]
    print(body)
    if not DISCORD_WEBHOOK:
        print("! DISCORD_WEBHOOK_URL not set - not posted")
        return
    try:
        resp = requests.post(DISCORD_WEBHOOK, json={"content": body}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        print("posted to Discord")
    except Exception as exc:  # noqa: BLE001
        print(f"! Discord post failed: {exc}")


def record_entries(config: dict, state: dict, week: str) -> None:
    benchmark = config.get("benchmark", "SPY")
    entries: dict[str, float] = {}

    bench_px = price(benchmark)
    if bench_px is None:
        print("! benchmark price unavailable - will retry next run")
        return
    entries[benchmark] = bench_px

    for call in config.get("calls") or []:
        ticker = call["ticker"].upper()
        px = price(ticker)
        if px is None:
            print(f"! no price for {ticker} - skipping this week")
            continue
        entries[ticker] = px

    state[week] = {"entries": entries, "entered_at": iso(now_utc()), "exited": False}

    lines = [f"**Calls locked - week of {week}**", ""]
    for call in config.get("calls") or []:
        ticker = call["ticker"].upper()
        if ticker not in entries:
            continue
        arrow = "UP" if call.get("direction", "up").lower() == "up" else "DOWN"
        author = call.get("author", "?")
        conf = call.get("confidence", "-")
        lines.append(f"`{sanitize(author)}` **{sanitize(ticker)}** {arrow} @ {entries[ticker]:.2f} (conf: {sanitize(conf)})")
        if call.get("reasoning"):
            lines.append(f"> {sanitize(call['reasoning'])}")
    lines += ["", f"Benchmark {benchmark} @ {bench_px:.2f}", "_Paper only. Scored Friday._"]
    post(lines)


def score_exits(config: dict, state: dict, week: str) -> None:
    benchmark = config.get("benchmark", "SPY")
    record = state[week]
    entries = record["entries"]

    bench_exit = price(benchmark)
    if bench_exit is None:
        print("! benchmark price unavailable - will retry next run")
        return
    bench_entry = entries[benchmark]
    bench_pct = (bench_exit - bench_entry) / bench_entry * 100

    rows: list[dict] = []
    lines = [f"**Scorecard - week of {week}**", "", f"Benchmark {benchmark}: {bench_pct:+.2f}%", ""]

    for call in config.get("calls") or []:
        ticker = call["ticker"].upper()
        if ticker not in entries:
            continue
        exit_px = price(ticker)
        if exit_px is None:
            continue
        entry_px = entries[ticker]
        pct = (exit_px - entry_px) / entry_px * 100
        direction = call.get("direction", "up").lower()
        # A "down" call is scored as a short: profit when the price falls.
        realised = pct if direction == "up" else -pct
        correct = (pct > 0) if direction == "up" else (pct < 0)
        vs_bench = realised - bench_pct

        mark = "OK " if correct else "MISS"
        lines.append(
            f"{mark} `{sanitize(call.get('author', '?'))}` **{sanitize(ticker)}** "
            f"{realised:+.2f}%  (vs bench {vs_bench:+.2f}%)"
        )

        rows.append({
            "week_of": week,
            "ticker": ticker,
            "author": call.get("author", ""),
            "direction": direction,
            "confidence": call.get("confidence", ""),
            "entry": f"{entry_px:.4f}",
            "exit": f"{exit_px:.4f}",
            "pct": f"{realised:.4f}",
            "benchmark_pct": f"{bench_pct:.4f}",
            "vs_benchmark": f"{vs_bench:.4f}",
            "correct": "yes" if correct else "no",
            "reasoning": call.get("reasoning", ""),
        })

    beat = sum(1 for r in rows if float(r["vs_benchmark"]) > 0)
    lines += ["", f"{beat}/{len(rows)} beat the benchmark.",
              "_Beating it over one week is noise. The tally is what matters._"]
    post(lines)

    exists = RESULTS_PATH.exists()
    with RESULTS_PATH.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=RESULT_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)

    record["exited"] = True
    record["exited_at"] = iso(now_utc())


def main() -> None:
    if not CALLS_PATH.exists():
        print("calls.yaml not found - nothing to do")
        return

    config = yaml.safe_load(CALLS_PATH.read_text()) or {}
    week = str(config.get("week_of", "")).strip()
    if not week or not (config.get("calls") or []):
        print("calls.yaml has no week_of or no calls - nothing to do")
        return

    now = datetime.now(ET)
    minutes = now.hour * 60 + now.minute
    state = load_state()
    record = state.get(week)

    print(f"ET now: {now:%Y-%m-%d %H:%M %a} | week_of: {week}")

    # Monday, any time from 9:35am ET onward: lock in entries (once).
    if now.weekday() == 0 and minutes >= 9 * 60 + 35 and record is None:
        print("-> recording entries")
        record_entries(config, state, week)
        save_state(state)
        return

    # Friday, from 3:45pm ET: score and close out (once).
    if now.weekday() == 4 and minutes >= 15 * 60 + 45 and record and not record.get("exited"):
        print("-> scoring exits")
        score_exits(config, state, week)
        save_state(state)
        return

    print("-> nothing to do this run")


if __name__ == "__main__":
    main()
