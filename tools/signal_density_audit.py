"""
Signal-density audit for the 9 burn-in strategies.

Sources:
  - DRY_RUN_VAULT/<vault_id>/<strategy_id>/run_snapshot/data/results_tradelevel.csv
    (canonical backtest trades — per-strategy)
  - DRY_RUN_VAULT/<vault_id>/index.json
    (trade count + date range from promotion)
  - TS_Execution/journal/SignalJournal.jsonl
    (live signals — for current portfolio, recent window)

Output: per-strategy + portfolio aggregate stats. No assumptions baked in;
just numbers from the canonical artifacts.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


PORTFOLIO_YAML = Path("C:/Users/faraw/Documents/TS_Execution/portfolio.yaml")
VAULT_ROOT     = Path("C:/Users/faraw/Documents/DRY_RUN_VAULT")
EXEC_JOURNAL   = Path("C:/Users/faraw/Documents/TS_Execution/journal/SignalJournal.jsonl")


def load_portfolio() -> list[dict]:
    with open(PORTFOLIO_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    out = []
    for entry in (cfg.get("portfolio") or {}).get("strategies", []):
        if entry.get("enabled", True):
            out.append({
                "id":       entry["id"],
                "symbol":   entry.get("symbol"),
                "timeframe": entry.get("timeframe"),
                "vault_id": entry.get("vault_id"),
            })
    return out


def load_vault_summary(strategy_id: str, vault_id: str) -> dict | None:
    """Read vault index.json for canonical backtest stats."""
    idx = VAULT_ROOT / vault_id / "index.json"
    if not idx.exists():
        return None
    with open(idx, encoding="utf-8") as f:
        full = json.load(f)
    sblock = (full.get("strategies") or {}).get(strategy_id)
    return sblock  # has: trades, date_start, date_end, pf, etc.


def load_vault_tradelevel(strategy_id: str, vault_id: str) -> list[dict]:
    """Read full per-trade canonical ledger from vault."""
    p = VAULT_ROOT / vault_id / strategy_id / "run_snapshot" / "data" / "results_tradelevel.csv"
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append(r)
    return out


def parse_canonical_dates(d_start: str, d_end: str) -> tuple[datetime, datetime, float]:
    sd = datetime.fromisoformat(d_start).replace(tzinfo=timezone.utc)
    ed = datetime.fromisoformat(d_end).replace(tzinfo=timezone.utc)
    days = max(1.0, (ed - sd).total_seconds() / 86400.0)
    return sd, ed, days


def parse_entry_ts(ts: str) -> datetime | None:
    try:
        # Vault format: "2024-01-04 10:00:00+00:00"
        dt = datetime.fromisoformat(ts.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def hour_of_day_dist(trades: list[dict]) -> Counter:
    c: Counter = Counter()
    for t in trades:
        dt = parse_entry_ts(t.get("entry_timestamp", ""))
        if dt:
            c[dt.hour] += 1
    return c


def weekday_dist(trades: list[dict]) -> Counter:
    c: Counter = Counter()
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for t in trades:
        dt = parse_entry_ts(t.get("entry_timestamp", ""))
        if dt:
            c[names[dt.weekday()]] += 1
    return c


def load_recent_live_signals(allowed_ids: set[str], cutoff_utc: datetime) -> dict[str, int]:
    """Count live signals per strategy in the recent window."""
    counts: dict[str, int] = defaultdict(int)
    if not EXEC_JOURNAL.exists():
        return dict(counts)
    with open(EXEC_JOURNAL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "event_type" in rec or "signal" not in rec:
                continue
            sid = rec.get("strategy_id")
            if sid not in allowed_ids:
                continue
            written = rec.get("written_utc", "")
            try:
                wdt = datetime.fromisoformat(written.replace("Z", "+00:00"))
                if wdt.tzinfo is None:
                    wdt = wdt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if wdt >= cutoff_utc:
                counts[sid] += 1
    return dict(counts)


def main() -> None:
    portfolio = load_portfolio()
    allowed_ids = {p["id"] for p in portfolio}
    now = datetime.now(timezone.utc)
    last_30d = now - timedelta(days=30)
    last_90d = now - timedelta(days=90)

    live_30d = load_recent_live_signals(allowed_ids, last_30d)
    live_90d = load_recent_live_signals(allowed_ids, last_90d)

    print("=" * 100)
    print("SIGNAL-DENSITY AUDIT  —  9 burn-in strategies")
    print(f"Generated: {now.isoformat(timespec='seconds')}")
    print("=" * 100)

    rows = []
    portfolio_total_per_day = 0.0
    portfolio_total_trades = 0
    portfolio_total_days = 0

    for s in portfolio:
        sid = s["id"]
        summary = load_vault_summary(sid, s["vault_id"]) or {}
        trades = summary.get("trades")
        d_start = summary.get("date_start")
        d_end   = summary.get("date_end")
        per_day = None
        per_week = None
        days = 0
        if trades is not None and d_start and d_end:
            _, _, days = parse_canonical_dates(d_start, d_end)
            per_day = trades / days
            per_week = per_day * 7
            portfolio_total_per_day += per_day
            portfolio_total_trades += trades
            portfolio_total_days += days

        rows.append({
            "id": sid,
            "symbol": s["symbol"],
            "tf": s["timeframe"],
            "vault_trades": trades,
            "vault_days": round(days, 1),
            "vault_per_day": round(per_day, 3) if per_day else None,
            "vault_per_week": round(per_week, 2) if per_week else None,
            "live_30d": live_30d.get(sid, 0),
            "live_90d": live_90d.get(sid, 0),
        })

    # Per-strategy table
    print(f"\n{'Strategy':<58s} {'Sym':<8s} {'TF':<5s} {'Vault':>6s} {'Days':>6s} {'/day':>7s} {'/week':>6s} {'L30d':>5s} {'L90d':>5s}")
    print("-" * 110)
    for r in sorted(rows, key=lambda x: -(x["vault_per_day"] or 0)):
        print(f"{r['id']:<58s} {r['symbol']:<8s} {r['tf']:<5s} "
              f"{(r['vault_trades'] or 0):>6d} {r['vault_days']:>6.0f} "
              f"{(r['vault_per_day'] or 0):>7.3f} {(r['vault_per_week'] or 0):>6.2f} "
              f"{r['live_30d']:>5d} {r['live_90d']:>5d}")

    print(f"\nPortfolio TOTAL signals/day (vault-implied): {portfolio_total_per_day:.3f}")
    print(f"Portfolio TOTAL signals/week (vault-implied): {portfolio_total_per_day*7:.2f}")
    print(f"Portfolio expected in 5 trading days (Mon-Fri): {portfolio_total_per_day*5:.2f}")
    print(f"Live last 30 days actual signals: {sum(live_30d.values())}")
    print(f"Live last 90 days actual signals: {sum(live_90d.values())}")

    # ---- Hour-of-day + weekday distribution (aggregated) ----
    hour_total: Counter = Counter()
    weekday_total: Counter = Counter()
    by_strategy_hours: dict[str, Counter] = {}

    for s in portfolio:
        trades = load_vault_tradelevel(s["id"], s["vault_id"])
        h = hour_of_day_dist(trades)
        wd = weekday_dist(trades)
        hour_total.update(h)
        weekday_total.update(wd)
        by_strategy_hours[s["id"]] = h

    print("\n" + "=" * 100)
    print("HOUR-OF-DAY DISTRIBUTION (entry_timestamp UTC, all 9 strategies aggregated)")
    print("=" * 100)
    total_h = sum(hour_total.values())
    if total_h > 0:
        for hr in range(24):
            n = hour_total.get(hr, 0)
            bar = "#" * int(40 * n / max(1, total_h) / 0.05) if n else ""
            print(f"  {hr:02d}:00  {n:>6d}  {100*n/total_h:>5.1f}%  {bar}")

    print("\n" + "=" * 100)
    print("WEEKDAY DISTRIBUTION (entry_timestamp, all 9 strategies aggregated)")
    print("=" * 100)
    total_w = sum(weekday_total.values())
    if total_w > 0:
        for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
            n = weekday_total.get(d, 0)
            bar = "#" * int(40 * n / total_w / 0.05) if n else ""
            print(f"  {d}  {n:>6d}  {100*n/total_w:>5.1f}%  {bar}")

    # ---- Time-to-30-signals projection ----
    print("\n" + "=" * 100)
    print("PROJECTION: time to accumulate N signals at vault-implied portfolio rate")
    print("=" * 100)
    if portfolio_total_per_day > 0:
        for n_target in [5, 10, 15, 20, 30, 50]:
            d_calendar = n_target / portfolio_total_per_day
            d_trading  = d_calendar * 7 / 5  # adjust if vault counted only weekdays
            print(f"  {n_target:>3d} signals: {d_calendar:>6.2f} calendar days "
                  f"(~{d_trading:>5.2f} trading days assuming weekend-blank)")


if __name__ == "__main__":
    main()
