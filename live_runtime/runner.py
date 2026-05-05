"""
runner.py — TS_Engine Phase B sidecar daemon.

Reads TS_Execution's portfolio.yaml to discover the active burn-in
strategies. Groups them by (symbol, timeframe). Spawns one thread per
group running an observer-only bar-close loop.

Each thread:
  - polls MT5 read-only at +20s after bar close (post TS_Execution)
  - calls v1.5.9 evaluate_bar() per strategy
  - writes shadow signals to journal/shadow_signal_journal.jsonl

Stop with Ctrl-C. No state in TS_Execution is touched.
"""

from __future__ import annotations

import argparse
import importlib
import signal as _signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

TS_ENGINE_ROOT = Path(__file__).resolve().parents[1]
TS_EXECUTION_ROOT = TS_ENGINE_ROOT.parent / "TS_Execution"
TRADE_SCAN_ROOT = TS_ENGINE_ROOT.parent / "Trade_Scan"

# Trade_Scan must be on path so strategies + engines import correctly
if str(TRADE_SCAN_ROOT) not in sys.path:
    sys.path.insert(0, str(TRADE_SCAN_ROOT))

from . import mt5_reader  # local
from .bar_loop import GroupConfig, run_group_loop  # local
from .shadow_journal import ShadowJournal  # local


PORTFOLIO_YAML = TS_EXECUTION_ROOT / "portfolio.yaml"
SHADOW_JOURNAL_PATH = TS_ENGINE_ROOT / "journal" / "shadow_signal_journal.jsonl"


# Timeframe period in seconds
_PERIOD_S = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


def _mt5_tf_to_lower(tf_mt5: str) -> str:
    m = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
         "H1": "1h", "H4": "4h", "D1": "1d"}
    return m.get(tf_mt5.strip().upper(), tf_mt5.lower())


def _load_portfolio_strategies() -> list[dict]:
    """Read portfolio.yaml; return list of {id, symbol, timeframe} for enabled strategies."""
    with open(PORTFOLIO_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    out = []
    for entry in (cfg.get("portfolio") or {}).get("strategies", []):
        if not entry.get("enabled", True):
            continue
        sid = entry.get("id")
        sym = entry.get("symbol")
        tf  = _mt5_tf_to_lower(entry.get("timeframe"))
        if sid and sym and tf:
            out.append({"id": sid, "symbol": sym, "timeframe": tf})
    return out


def _load_strategy_instance(strategy_id: str):
    """Dynamically load Strategy class from strategies/<id>/strategy.py."""
    module_path = f"strategies.{strategy_id}.strategy"
    module = importlib.import_module(module_path)
    StrategyClass = getattr(module, "Strategy", None)
    if StrategyClass is None:
        raise ValueError(f"Strategy class not found in {module_path}")
    return StrategyClass()


def _build_groups(strategies: list[dict]) -> list[GroupConfig]:
    """Group strategies by (symbol, timeframe). One bar loop per group."""
    by_key: dict[tuple[str, str], list[dict]] = {}
    for s in strategies:
        key = (s["symbol"], s["timeframe"])
        by_key.setdefault(key, []).append(s)

    groups: list[GroupConfig] = []
    for (sym, tf), members in by_key.items():
        if tf not in _PERIOD_S:
            print(f"  skip group {sym}/{tf}: unsupported timeframe")
            continue
        instances = []
        ids = []
        for s in members:
            try:
                inst = _load_strategy_instance(s["id"])
                instances.append(inst)
                ids.append(s["id"])
            except Exception as e:
                print(f"  STRATEGY_LOAD_FAILED {s['id']}: "
                      f"{type(e).__name__}: {e}")
        if not instances:
            continue
        groups.append(GroupConfig(
            symbol=sym, timeframe=tf, period_s=_PERIOD_S[tf],
            strategies=instances, strategy_ids=ids,
        ))
    return groups


def _connect_mt5(args) -> bool:
    """Initialize read-only MT5 connection. Login is OPTIONAL — if not provided,
    we attach to whatever terminal is already running (TS_Execution's terminal)."""
    print("  connecting to MT5 (read-only)...")
    ok = mt5_reader.initialize(
        login=args.mt5_login,
        server=args.mt5_server,
        password=args.mt5_password,
        path=args.mt5_path,
    )
    if not ok:
        print(f"  MT5 init failed: {mt5_reader.last_error()}")
        return False
    info = mt5_reader.account_info()
    if info is not None:
        print(f"  MT5 connected: account={info.login}  server={info.server}  "
              f"balance={info.balance}")
    else:
        print("  MT5 connected (account_info unavailable)")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="TS_Engine Phase B live shadow sidecar")
    p.add_argument("--mt5-login",    type=int, default=None,
                   help="MT5 account login (default: attach to running terminal)")
    p.add_argument("--mt5-server",   default=None, help="MT5 server name")
    p.add_argument("--mt5-password", default=None, help="MT5 password")
    p.add_argument("--mt5-path",     default=None, help="MT5 terminal exe path")
    p.add_argument("--run-id",       default=None, help="Run identifier (default: timestamp)")
    args = p.parse_args()

    run_id = args.run_id or f"PHASE_B_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    print("=" * 78)
    print(f"TS_Engine Phase B sidecar — run_id={run_id}")
    print("Observer-only mode. Zero dispatch authority.")
    print("=" * 78)

    print(f"\n[1/4] Loading portfolio from {PORTFOLIO_YAML}")
    strategies = _load_portfolio_strategies()
    print(f"  found {len(strategies)} enabled strategies")

    print(f"\n[2/4] Building (symbol, tf) groups...")
    groups = _build_groups(strategies)
    print(f"  built {len(groups)} groups:")
    for g in groups:
        print(f"    {g.symbol} {g.timeframe}: {len(g.strategy_ids)} strategies")

    if not groups:
        print("  no valid groups; exiting")
        return 1

    print(f"\n[3/4] Connecting to MT5 (read-only)...")
    if not _connect_mt5(args):
        return 1

    print(f"\n[4/4] Spawning bar loops...")
    journal = ShadowJournal(SHADOW_JOURNAL_PATH, run_id=run_id)
    journal.write_marker("RUN_START", detail=run_id)

    stop_flag = threading.Event()

    def _on_signal(signum, _frame):
        print(f"\n  signal {signum} received — stopping all loops...")
        stop_flag.set()
    _signal.signal(_signal.SIGINT,  _on_signal)
    if hasattr(_signal, "SIGTERM"):
        _signal.signal(_signal.SIGTERM, _on_signal)

    threads = []
    for g in groups:
        t = threading.Thread(
            target=run_group_loop,
            args=(g, journal, stop_flag),
            name=f"barloop-{g.symbol}-{g.timeframe}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        print(f"  started thread: {t.name}")

    # Main loop: keep running until stop_flag is set
    print("\n" + "=" * 78)
    print("Phase B sidecar running. Ctrl-C to stop.")
    print("=" * 78)
    try:
        while not stop_flag.is_set():
            time.sleep(1.0)
            # Periodic liveness check
            for t in threads:
                if not t.is_alive():
                    print(f"  THREAD_DEAD: {t.name}")
    except KeyboardInterrupt:
        stop_flag.set()

    print("  waiting for loops to finish...")
    for t in threads:
        t.join(timeout=30)

    journal.write_marker("RUN_END", detail=run_id)
    mt5_reader.shutdown()
    print("  TS_Engine sidecar stopped cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
