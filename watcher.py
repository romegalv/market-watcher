#!/usr/bin/env python3
"""
Read-only market and wallet watcher.

Checks price rules and on-chain wallet activity, appends every signal to
signals.csv, and posts a summary to a Discord webhook.

THIS PROGRAM CANNOT TRADE. It holds no brokerage credentials and makes no
call that can move money. The worst failure mode is that it stops alerting.

Environment variables (set as GitHub repository secrets):
    DISCORD_WEBHOOK_URL   required
    HELIUS_API_KEY        optional, needed only for Solana wallets
    ETHERSCAN_API_KEY     optional, needed only for EVM wallets
"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "signals.csv"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
HELIUS_KEY = os.environ.get("HELIUS_API_KEY", "").strip()
ETHERSCAN_KEY = os.environ.get("ETHERSCAN_API_KEY", "").strip()

LOG_FIELDS = ["ts_utc", "source", "kind", "asset", "detail", "price_at_signal", "ref"]
HTTP_TIMEOUT = 20


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit("config.yaml not found next to watcher.py")
    with CONFIG_PATH.open() as fh:
        return yaml.safe_load(fh) or {}


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen_tx": {}, "last_alert": {}}
    try:
        with STATE_PATH.open() as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"seen_tx": {}, "last_alert": {}}
    state.setdefault("seen_tx", {})
    state.setdefault("last_alert", {})
    return state


def save_state(state: dict) -> None:
    # Cap the per-wallet dedupe list so the file cannot grow without bound.
    for addr, refs in state.get("seen_tx", {}).items():
        state["seen_tx"][addr] = refs[-400:]
    with STATE_PATH.open("w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def cooled_down(state: dict, key: str, minutes: int) -> bool:
    """True if enough time has passed since this rule last fired."""
    last = state["last_alert"].get(key)
    if not last:
        return True
    try:
        prev = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (now_utc() - prev).total_seconds() >= minutes * 60


def mark_fired(state: dict, key: str) -> None:
    state["last_alert"][key] = iso(now_utc())


# --------------------------------------------------------------------------
# prices
# --------------------------------------------------------------------------

def equity_price(symbol: str) -> tuple[float, float] | None:
    """Return (last_price, previous_close) or None if unavailable."""
    try:
        import yfinance as yf

        info = yf.Ticker(symbol).fast_info
        last = float(info["last_price"])
        prev = float(info["previous_close"])
        if last <= 0 or prev <= 0:
            return None
        return last, prev
    except Exception as exc:  # noqa: BLE001 - a data hiccup must not kill the run
        print(f"  ! equity price failed for {symbol}: {exc}")
        return None


def crypto_price(coingecko_id: str) -> tuple[float, float] | None:
    """Return (last_price, price_24h_ago) or None if unavailable."""
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": coingecko_id,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        row = resp.json().get(coingecko_id)
        if not row:
            return None
        last = float(row["usd"])
        change = float(row.get("usd_24h_change") or 0.0)
        prev = last / (1 + change / 100) if change != -100 else last
        return last, prev
    except Exception as exc:  # noqa: BLE001
        print(f"  ! crypto price failed for {coingecko_id}: {exc}")
        return None


def check_price_rules(config: dict, state: dict) -> list[dict]:
    signals: list[dict] = []
    default_cooldown = int(config.get("alert_cooldown_minutes", 240))

    for entry in config.get("tickers") or []:
        symbol = entry["symbol"]
        kind = entry.get("kind", "equity")

        if kind == "crypto":
            quote = crypto_price(entry.get("coingecko_id", symbol.lower()))
        else:
            quote = equity_price(symbol)

        if quote is None:
            continue
        last, prev = quote
        pct = (last - prev) / prev * 100 if prev else 0.0
        print(f"  {symbol}: {last:.4f} ({pct:+.2f}%)")

        for idx, rule in enumerate(entry.get("rules") or []):
            rtype = rule.get("type")
            key = f"{symbol}:{idx}"
            cooldown = int(rule.get("cooldown_minutes", default_cooldown))
            hit = False
            detail = ""

            if rtype == "pct_move":
                threshold = float(rule["threshold"])
                if abs(pct) >= threshold:
                    hit = True
                    direction = "up" if pct > 0 else "down"
                    detail = f"moved {direction} {abs(pct):.2f}% (threshold {threshold}%)"
            elif rtype == "above":
                target = float(rule["price"])
                if last > target:
                    hit = True
                    detail = f"trading above {target}"
            elif rtype == "below":
                target = float(rule["price"])
                if last < target:
                    hit = True
                    detail = f"trading below {target}"
            else:
                print(f"  ! unknown rule type {rtype!r} on {symbol}")
                continue

            if hit and cooled_down(state, key, cooldown):
                mark_fired(state, key)
                signals.append(
                    {
                        "source": "price",
                        "kind": rtype,
                        "asset": symbol,
                        "detail": detail,
                        "price_at_signal": f"{last:.6f}",
                        "ref": "",
                    }
                )

    return signals


# --------------------------------------------------------------------------
# wallets
# --------------------------------------------------------------------------

def check_solana(wallets: list[dict], state: dict, limit: int) -> list[dict]:
    if not wallets:
        return []
    if not HELIUS_KEY:
        print("  ! Solana wallets configured but HELIUS_API_KEY is not set - skipping")
        return []

    signals: list[dict] = []
    for wallet in wallets:
        address = wallet["address"]
        label = wallet.get("label", address[:6])
        seen = state["seen_tx"].setdefault(address, [])
        try:
            resp = requests.get(
                f"https://api.helius.xyz/v0/addresses/{address}/transactions",
                params={"api-key": HELIUS_KEY, "limit": limit},
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            txs = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Solana fetch failed for {label}: {exc}")
            continue

        watch_types = {t.upper() for t in (wallet.get("types") or ["SWAP"])}
        fresh = [t for t in txs if t.get("signature") not in seen]
        # Oldest first so the log reads chronologically.
        for tx in reversed(fresh):
            sig = tx.get("signature")
            if sig:
                seen.append(sig)
            tx_type = (tx.get("type") or "UNKNOWN").upper()
            if watch_types and tx_type not in watch_types:
                continue
            signals.append(
                {
                    "source": f"solana:{label}",
                    "kind": tx_type,
                    "asset": "",
                    "detail": (tx.get("description") or "no description")[:300],
                    "price_at_signal": "",
                    "ref": f"https://solscan.io/tx/{sig}",
                }
            )
        print(f"  {label}: {len(fresh)} new tx")

    return signals


def check_evm(wallets: list[dict], state: dict, limit: int) -> list[dict]:
    if not wallets:
        return []
    if not ETHERSCAN_KEY:
        print("  ! EVM wallets configured but ETHERSCAN_API_KEY is not set - skipping")
        return []

    chain_ids = {"ethereum": 1, "base": 8453, "arbitrum": 42161, "polygon": 137}
    explorers = {
        1: "https://etherscan.io/tx/",
        8453: "https://basescan.org/tx/",
        42161: "https://arbiscan.io/tx/",
        137: "https://polygonscan.com/tx/",
    }

    signals: list[dict] = []
    for wallet in wallets:
        address = wallet["address"].lower()
        label = wallet.get("label", address[:8])
        chain = wallet.get("chain", "ethereum").lower()
        chain_id = chain_ids.get(chain)
        if chain_id is None:
            print(f"  ! unsupported chain {chain!r} for {label}")
            continue

        seen = state["seen_tx"].setdefault(address, [])
        try:
            resp = requests.get(
                "https://api.etherscan.io/v2/api",
                params={
                    "chainid": chain_id,
                    "module": "account",
                    "action": "tokentx",
                    "address": address,
                    "page": 1,
                    "offset": limit,
                    "sort": "desc",
                    "apikey": ETHERSCAN_KEY,
                },
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            transfers = payload.get("result") or []
            if not isinstance(transfers, list):
                print(f"  ! Etherscan said: {payload.get('message')}")
                continue
        except Exception as exc:  # noqa: BLE001
            print(f"  ! EVM fetch failed for {label}: {exc}")
            continue

        fresh = [t for t in transfers if t.get("hash") not in seen]
        for tx in reversed(fresh):
            tx_hash = tx.get("hash")
            if tx_hash:
                seen.append(tx_hash)
            try:
                decimals = int(tx.get("tokenDecimal") or 18)
                amount = int(tx.get("value") or 0) / (10 ** decimals)
            except (ValueError, TypeError):
                amount = 0.0
            direction = "received" if tx.get("to", "").lower() == address else "sent"
            token = (tx.get("tokenSymbol") or "?")[:20]
            signals.append(
                {
                    "source": f"{chain}:{label}",
                    "kind": "TOKEN_TRANSFER",
                    "asset": token,
                    "detail": f"{direction} {amount:,.4f} {token}",
                    "price_at_signal": "",
                    "ref": explorers.get(chain_id, "") + str(tx_hash),
                }
            )
        print(f"  {label} ({chain}): {len(fresh)} new transfers")

    return signals


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def append_log(signals: list[dict]) -> None:
    if not signals:
        return
    exists = LOG_PATH.exists()
    with LOG_PATH.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if not exists:
            writer.writeheader()
        stamp = iso(now_utc())
        for sig in signals:
            writer.writerow({"ts_utc": stamp, **sig})


def sanitize(text: str) -> str:
    """Neutralise Discord markdown and stray mentions in untrusted strings.

    Token names and tx descriptions are attacker-controlled. This keeps a
    hostile token name from formatting itself into something misleading or
    pinging everyone in the channel.
    """
    cleaned = text.replace("@everyone", "@ everyone").replace("@here", "@ here")
    for char in ("`", "*", "_", "~", "|", ">"):
        cleaned = cleaned.replace(char, "")
    return cleaned[:300]


def post_discord(signals: list[dict]) -> None:
    if not signals:
        print("no signals this run")
        return
    if not DISCORD_WEBHOOK:
        print("! DISCORD_WEBHOOK_URL not set - logged but not sent")
        return

    lines = [f"**{len(signals)} signal(s)** - {iso(now_utc())}", ""]
    for sig in signals[:15]:
        line = f"`{sanitize(sig['source'])}` {sanitize(sig['detail'])}"
        if sig.get("price_at_signal"):
            line += f"  (px {sig['price_at_signal']})"
        if sig.get("ref"):
            line += f"\n<{sig['ref']}>"
        lines.append(line)
    if len(signals) > 15:
        lines.append(f"...and {len(signals) - 15} more (see signals.csv)")
    lines += ["", "_Read-only watcher. No trades were or can be placed._"]

    try:
        resp = requests.post(
            DISCORD_WEBHOOK,
            json={"content": "\n".join(lines)[:1900]},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        print(f"posted {len(signals)} signal(s) to Discord")
    except Exception as exc:  # noqa: BLE001
        print(f"! Discord post failed: {exc}")


# --------------------------------------------------------------------------

def main() -> None:
    config = load_config()
    state = load_state()
    limit = int(config.get("wallet_tx_limit", 25))

    print("== prices ==")
    signals = check_price_rules(config, state)

    wallets = config.get("wallets") or {}
    print("== solana ==")
    signals += check_solana(wallets.get("solana") or [], state, limit)
    print("== evm ==")
    signals += check_evm(wallets.get("evm") or [], state, limit)

    append_log(signals)
    post_discord(signals)
    save_state(state)


if __name__ == "__main__":
    main()
