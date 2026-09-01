#!/usr/bin/env python3
"""
Multi-source news scanner.

Reads primary sources (SEC 8-K filings, PR wires) plus journalism feeds,
extracts which ticker each headline is about, scores likely price impact,
records the price at that moment, and later backfills what the price did.

The backfill is the point. A score with no follow-up is an opinion. A score
sitting next to the move that followed is a testable claim. After a month,
open news_log.csv and check whether high scores actually preceded large moves.
If they don't correlate, the number is decoration - and you'll know cheaply.

Cannot trade. No brokerage credentials anywhere in this program.

Environment:
    DISCORD_WEBHOOK_URL      required
    ANTHROPIC_API_KEY        required for scoring
    ANTHROPIC_WORKSPACE_ID   required if your API key is workspace-scoped
"""

from __future__ import annotations

import csv
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

try:
    # news.py reuses watcher.py's Discord plumbing and price helpers, so the two
    # files must sit in the same folder.
    from watcher import DISCORD_WEBHOOK, HTTP_TIMEOUT, equity_price, iso, now_utc, sanitize
except ImportError as _exc:
    _bar = "=" * 60
    raise SystemExit(
        f"\n{_bar}\n"
        f"  Cannot import watcher.py: {_exc}\n"
        f"  news.py must sit in the SAME folder as watcher.py (the repo root),\n"
        f"  not inside a subfolder. Check the Code tab: watcher.py, news.py,\n"
        f"  feeds.yaml and config.yaml should all be listed side by side.\n"
        f"{_bar}"
    )

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
FEEDS_PATH = ROOT / "feeds.yaml"
STATE_PATH = ROOT / "news_state.json"
LOG_PATH = ROOT / "news_log.csv"

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = "claude-haiku-4-5-20251001"

# Workspace-scoped ("identity-linked") API keys must name the workspace the
# request acts in. Keys created at the default org level don't need this, so
# leave the secret unset if yours works without it.
WORKSPACE_ID = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()

LOG_FIELDS = [
    "ts_utc", "ticker", "score", "direction", "source", "price_at_scan",
    "price_1h", "move_1h_pct", "price_24h", "move_24h_pct",
    "rationale", "headline", "link",
]

SYSTEM = """You read financial news headlines and identify the affected US-listed
company and the likely short-term price impact.

Return ONLY a JSON object. No prose, no markdown fences:
{"ticker": "<US ticker or null>", "score": <1-100>, "direction": "up"|"down"|"unclear", "rationale": "<12 words max>"}

ticker: the US-listed ticker most affected. null if the headline names no
specific public company, names a private company, or is macro/sector-wide.
Never guess a ticker you are not confident about - null is correct far more
often than a wrong guess.

score: expected magnitude of price move for that ticker over the next 1-2
sessions.
  1-20   routine: analyst notes, recycled coverage, minor product news
  21-50  moderate: sector news, small contracts, guidance tweaks
  51-75  significant: earnings surprise, major contract, regulatory action,
         executive departure, offering
  76-100 severe: bankruptcy, fraud allegation, M&A, trial results, halt

Be conservative. Most headlines are noise and belong under 20. Reserve 76+
for genuinely rare events. Promotional or aggregated content scores low.
Use "unclear" for direction whenever the sign is genuinely ambiguous - a
merger target and acquirer move opposite ways, for instance."""


# --------------------------------------------------------------------------
# feeds
# --------------------------------------------------------------------------

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """Handle both RSS <item> and Atom <entry> without a third-party parser."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    out = []
    for item in root.iter("item"):                      # RSS
        title = strip_html(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        if title and link:
            out.append({"title": title, "link": link})
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):   # Atom
        title = strip_html(entry.findtext("{http://www.w3.org/2005/Atom}title") or "")
        link_el = entry.find("{http://www.w3.org/2005/Atom}link")
        link = (link_el.get("href") if link_el is not None else "") or ""
        if title and link:
            out.append({"title": title, "link": link.strip()})
    return out


def fetch_feed(url: str, agent: str) -> list[dict]:
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": agent})
        resp.raise_for_status()
        return parse_feed(resp.content)
    except Exception as exc:  # noqa: BLE001 - one dead feed must not kill the run
        print(f"    ! {exc}")
        return []


def gather(feeds_cfg: dict, tickers: list[str], seen: set[str]) -> list[dict]:
    agent = feeds_cfg.get("user_agent", "market-watcher/1.0")
    noise = [n.lower() for n in (feeds_cfg.get("noise_filters") or [])]
    collected: list[dict] = []

    for feed in feeds_cfg.get("feeds") or []:
        if not feed.get("enabled", True):
            continue
        label = feed.get("label", "?")
        urls = (
            [(feed["url"].replace("{ticker}", t), t) for t in tickers]
            if feed.get("per_ticker")
            else [(feed["url"], None)]
        )

        found = 0
        per_feed_cap = int(feed.get("max_items", 6 if feed.get("per_ticker") else 40))
        for url, hint in urls:
            kept_here = 0
            for item in fetch_feed(url, agent):
                if kept_here >= per_feed_cap:
                    break
                if item["link"] in seen:
                    continue
                seen.add(item["link"])
                if any(n in item["title"].lower() for n in noise):
                    continue
                collected.append({
                    "title": item["title"],
                    "link": item["link"],
                    "source": label,
                    "weight": int(feed.get("weight", 1)),
                    "hint": hint,
                })
                found += 1
                kept_here += 1
        print(f"  {label}: {found} new")

    # Upstream sources first, so the per-run budget goes to filings and wires
    # before it goes to commentary.
    collected.sort(key=lambda i: -i["weight"])
    return collected


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if WORKSPACE_ID:
        headers["anthropic-workspace-id"] = WORKSPACE_ID
    return headers


def score(headline: str, hint: str | None) -> dict | None:
    if not ANTHROPIC_KEY:
        print("  ! ANTHROPIC_API_KEY not set - cannot score")
        return None
    prompt = f"Headline: {headline}"
    if hint:
        prompt += f"\n(This came from a feed specific to {hint}.)"
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=_headers(),
            json={
                "model": MODEL,
                "max_tokens": 200,
                "system": SYSTEM,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            # Anthropic returns a JSON body explaining exactly what was wrong.
            # raise_for_status() would throw that away, which is how a 400 turns
            # into an unhelpful mystery.
            detail = resp.text[:400]
            raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
        text = "".join(
            b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text"
        )
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        parsed = json.loads(match.group(0))
        ticker = parsed.get("ticker")
        ticker = str(ticker).upper().strip() if ticker else None
        if ticker and not re.fullmatch(r"[A-Z.\-]{1,6}", ticker):
            ticker = None
        return {
            "ticker": ticker,
            "score": max(1, min(100, int(parsed.get("score", 1)))),
            "direction": str(parsed.get("direction", "unclear")).lower(),
            "rationale": str(parsed.get("rationale", ""))[:120],
        }
    except Exception as exc:  # noqa: BLE001
        print(f"  ! scoring failed: {exc}")
        return None


# --------------------------------------------------------------------------
# state, backfill, output
# --------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen": [], "pending": []}
    try:
        state = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"seen": [], "pending": []}
    state.setdefault("seen", [])
    state.setdefault("pending", [])
    return state


def save_state(state: dict) -> None:
    state["seen"] = state["seen"][-4000:]
    STATE_PATH.write_text(json.dumps(state, indent=2))


def backfill(state: dict) -> list[dict]:
    done, pending = [], []
    now = now_utc()
    for item in state["pending"]:
        try:
            scanned = datetime.strptime(item["ts_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        age = now - scanned
        entry = item.get("price_at_scan")

        if age >= timedelta(hours=1) and not item.get("price_1h") and entry:
            quote = equity_price(item["ticker"])
            if quote:
                item["price_1h"] = f"{quote[0]:.4f}"
                item["move_1h_pct"] = f"{(quote[0] - float(entry)) / float(entry) * 100:.4f}"

        if age >= timedelta(hours=24):
            if not item.get("price_24h") and entry:
                quote = equity_price(item["ticker"])
                if quote:
                    item["price_24h"] = f"{quote[0]:.4f}"
                    item["move_24h_pct"] = f"{(quote[0] - float(entry)) / float(entry) * 100:.4f}"
            done.append(item)
        else:
            pending.append(item)
    state["pending"] = pending
    return done


def append_log(rows: list[dict]) -> None:
    if not rows:
        return
    exists = LOG_PATH.exists()
    with LOG_PATH.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in LOG_FIELDS})


def post(items: list[dict], threshold: int) -> None:
    hits = [i for i in items if i["score"] >= threshold and i.get("ticker")]
    if not hits:
        print(f"nothing at or above {threshold} with a resolved ticker")
        return
    lines = [f"**News - {len(hits)} scored {threshold}+**", ""]
    for item in sorted(hits, key=lambda i: -i["score"])[:8]:
        arrow = {"up": "UP", "down": "DOWN"}.get(item["direction"], "?")
        lines.append(
            f"**{item['score']}** `{sanitize(item['ticker'])}` {arrow} "
            f"[{sanitize(item['source'])}] {sanitize(item['headline'])}"
        )
        if item.get("rationale"):
            lines.append(f"> {sanitize(item['rationale'])}")
        lines.append(f"<{item['link']}>")
    lines += ["", "_Untested model estimates, not measurements. news_log.csv shows whether they track real moves._"]

    if not DISCORD_WEBHOOK:
        print("\n".join(lines))
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": "\n".join(lines)[:1900]},
                      timeout=HTTP_TIMEOUT).raise_for_status()
        print(f"posted {len(hits)} headline(s)")
    except Exception as exc:  # noqa: BLE001
        print(f"! Discord post failed: {exc}")


def preflight() -> bool:
    """Check the environment and report every problem at once, in plain language."""
    problems = []
    if not FEEDS_PATH.exists():
        problems.append("feeds.yaml is missing - upload it next to news.py in the repo root")
    if not CONFIG_PATH.exists():
        problems.append("config.yaml is missing - news.py reads your ticker list from it")
    if not (ROOT / "watcher.py").exists():
        problems.append("watcher.py is missing - news.py imports shared helpers from it")
    if not ANTHROPIC_KEY:
        problems.append("ANTHROPIC_API_KEY secret is not set - feeds will be read but nothing scored")
    if not DISCORD_WEBHOOK:
        problems.append("DISCORD_WEBHOOK_URL secret is not set - results will print here only")
    if ANTHROPIC_KEY and not WORKSPACE_ID:
        print("  note: ANTHROPIC_WORKSPACE_ID not set. Only needed if your API key "
              "is workspace-scoped; the error message will say so if it is.")

    if problems:
        print("=" * 60)
        for problem in problems:
            print(f"  SETUP: {problem}")
        print("=" * 60)
    # Missing files are fatal; missing secrets only degrade the run.
    return FEEDS_PATH.exists() and CONFIG_PATH.exists()


def main() -> None:
    if not preflight():
        print("stopping - fix the file problems above and re-run")
        return

    try:
        feeds_cfg = yaml.safe_load(FEEDS_PATH.read_text()) or {}
        config = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except yaml.YAMLError as exc:
        print(f"! a YAML file is malformed: {exc}")
        return
    tickers = [
        t["symbol"] for t in (config.get("tickers") or [])
        if t.get("kind", "equity") == "equity"
    ]

    state = load_state()
    seen = set(state["seen"])

    print("== backfill ==")
    completed = backfill(state)
    append_log(completed)
    print(f"  {len(completed)} completed the 24h window")

    print("== feeds ==")
    candidates = gather(feeds_cfg, tickers, seen)
    print(f"  {len(candidates)} candidate headlines after noise filter")

    print("== scoring ==")
    budget = int(feeds_cfg.get("max_scored_per_run", 12))
    scored: list[dict] = []

    consecutive_failures = 0
    for cand in candidates:
        if budget <= 0:
            break
        if consecutive_failures >= 3:
            print("  ! 3 scoring calls failed in a row - stopping to avoid "
                  "burning credits on a broken request. Fix the error above.")
            break
        result = score(cand["title"], cand["hint"])
        if result is None:
            consecutive_failures += 1
            continue
        consecutive_failures = 0
        budget -= 1
        if not result["ticker"]:
            print(f"    [ -- ] no ticker: {cand['title'][:55]}")
            continue

        quote = equity_price(result["ticker"])
        if quote is None:
            print(f"    [ -- ] unresolvable ticker {result['ticker']}")
            continue

        item = {
            "ts_utc": iso(now_utc()),
            "headline": cand["title"],
            "link": cand["link"],
            "source": cand["source"],
            "price_at_scan": f"{quote[0]:.4f}",
            **result,
        }
        scored.append(item)
        state["pending"].append(item)
        print(f"    [{result['score']:3}] {result['ticker']:6} {result['direction']:7} {cand['title'][:45]}")

    state["seen"] = list(seen)
    post(scored, int(feeds_cfg.get("post_threshold", 60)))
    save_state(state)


if __name__ == "__main__":
    main()
