"""
TS_Engine/tools/kalman_replay.py — strict zero-edits H1/H4 investigation harness.

Purpose
-------
Deterministic offline replay of strategy 62_TREND_IDX_5M_KALFLIP_S01_V2_P15
over the same NAS100 5m archived bars the live sidecar saw, using two
independent runs that differ ONLY in the length of warmup history fed to the
filter:

    Replay A  —  72 h preceding warmup  (simulates sidecar age ~3 d at div #5)
    Replay B  —  12 h preceding warmup  (simulates execution age ~11 h at div #5)

Both replays then advance forward bar-by-bar over an identical live tape that
spans 2026-05-04 12:00 -> 22:30 UTC, capturing fire / no-fire decisions and
publicly-exposed Kalman state (kalman_trend / kalman_regime / kalman_flip
columns from `strategy.prepare_indicators`) at the four bars of interest:

    2026-05-04 12:15 UTC   (divergence #5)
    2026-05-04 15:00 UTC   (clean comparable)
    2026-05-04 20:20 UTC   (clean comparable)
    2026-05-04 22:10 UTC   (divergence #6)

Constraints (per directive)
---------------------------
- Strict zero-edits: no engine, strategy, or indicator code changes.
- No debug hooks: only public DataFrame columns and BarState attributes.
- No live restarts: live sidecar (PID 34676) and parity monitor (PID 30128)
  remain stopped per Phase B termination doctrine; TS_Execution +
  watchdog continue running unaffected.
- Public engine surfaces only:
    engine_dev.universal_research_engine.v1_5_9.evaluate_bar
        - evaluate_bar(df, i, state, strategy, config)
        - BarState
        - EngineConfig
        - resolve_engine_config
    engines.regime_state_machine.apply_regime_model
    strategies.62_TREND_IDX_5M_KALFLIP_S01_V2_P15.strategy.Strategy

Outputs
-------
    runtime_logs/archive_phase_b_final/kalman_investigation/
        REPLAY_A_72h.jsonl
        REPLAY_B_12h.jsonl
        kalman_replay_summary.json

Verdict mapping
---------------
- Replay A and Replay B differ on at least one of the divergence bars
  (12:15 / 22:10 UTC) AND match production direction (sidecar skip / execution
  fire) -> H1 confirmed.
- Replay A and Replay B agree on all four target bars -> H1 not reproduced.
- Outputs internally inconsistent or unexplained -> H4 still possible.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Path resolution — vault FIRST so v1_5_9 wins over Trade_Scan/engine_dev
# (which only carries v1_5_3..v1_5_8). Trade_Scan SECOND so engines / strategies
# / indicators packages resolve.
# ---------------------------------------------------------------------------
HARNESS_FILE      = Path(__file__).resolve()
TS_ENGINE_ROOT    = HARNESS_FILE.parents[1]
TRADE_SCAN_ROOT   = TS_ENGINE_ROOT.parent / "Trade_Scan"
VAULT_SNAPSHOT    = (
    TRADE_SCAN_ROOT
    / "vault" / "snapshots" / "DR_BASELINE_2026_05_03_v1_5_8a"
)
DATA_CSV = (
    TS_ENGINE_ROOT.parent
    / "Anti_Gravity_DATA_ROOT"
    / "MASTER_DATA"
    / "NAS100_OCTAFX_MASTER"
    / "RESEARCH"
    / "NAS100_OCTAFX_5m_2026_RESEARCH.csv"
)
DEFAULT_OUT_DIR = (
    TS_ENGINE_ROOT
    / "runtime_logs" / "archive_phase_b_final" / "kalman_investigation"
)

# Insert in reverse so VAULT_SNAPSHOT ends up at sys.path[0] and resolves
# `engine_dev.universal_research_engine.v1_5_9` first (Trade_Scan/engine_dev/
# carries v1_5_3..v1_5_8 only). TRADE_SCAN_ROOT remains accessible for
# `engines`, `indicators`, `strategies`.
for _p in (TRADE_SCAN_ROOT, VAULT_SNAPSHOT):
    sp = str(_p)
    if sp in sys.path:
        sys.path.remove(sp)
    sys.path.insert(0, sp)

from engine_dev.universal_research_engine.v1_5_9.evaluate_bar import (  # type: ignore  # noqa: E402
    BarState,
    EngineConfig,  # noqa: F401  (re-exported for callers)
    evaluate_bar,
    resolve_engine_config,
)
from engines.regime_state_machine import apply_regime_model  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STRATEGY_ID = "62_TREND_IDX_5M_KALFLIP_S01_V2_P15"

TARGET_BARS_UTC = [
    "2026-05-04 12:15:00",
    "2026-05-04 15:00:00",
    "2026-05-04 20:20:00",
    "2026-05-04 22:10:00",
]

LIVE_WINDOW_START = pd.Timestamp("2026-05-04 12:00:00", tz="UTC")
LIVE_WINDOW_END   = pd.Timestamp("2026-05-04 22:30:00", tz="UTC")

# Map for production reference (what each bar showed in the live system)
LIVE_REFERENCE = {
    "2026-05-04 12:15:00": {"sidecar": "ABSENT",  "execution": "PRESENT", "class": "DIVERGENCE_5"},
    "2026-05-04 15:00:00": {"sidecar": "PRESENT", "execution": "PRESENT", "class": "COMPARABLE"},
    "2026-05-04 20:20:00": {"sidecar": "PRESENT", "execution": "PRESENT", "class": "COMPARABLE"},
    "2026-05-04 22:10:00": {"sidecar": "ABSENT",  "execution": "PRESENT", "class": "DIVERGENCE_6"},
}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_strategy() -> Any:
    """Load a fresh Strategy instance via the same import path the sidecar uses."""
    mod = importlib.import_module(f"strategies.{STRATEGY_ID}.strategy")
    return mod.Strategy()


def load_bars(csv_path: Path) -> pd.DataFrame:
    """Read research CSV (skipping `#` metadata header), set UTC DatetimeIndex."""
    df = pd.read_csv(csv_path, comment="#")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    # Engine convention: ensure required columns are present
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"CSV missing required columns: {missing}")
    return df


def slice_window(df: pd.DataFrame, warmup_hours: float,
                 live_start: pd.Timestamp, live_end: pd.Timestamp) -> pd.DataFrame:
    """Return df rows from (live_start - warmup_hours) -> live_end inclusive."""
    start = live_start - timedelta(hours=warmup_hours)
    return df.loc[(df.index >= start) & (df.index <= live_end)].copy()


# ---------------------------------------------------------------------------
# Core replay
# ---------------------------------------------------------------------------

def _scalar_or_none(v: Any) -> Any:
    """Convert pandas/numpy scalar to native or None for JSON."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            pass
    return v


def replay(label: str, warmup_hours: float, df_full: pd.DataFrame,
           live_start: pd.Timestamp, live_end: pd.Timestamp) -> dict[str, Any]:
    """
    One replay run.

    Mechanic:
      * slice df_full to [live_start - warmup_hours, live_end]
      * call strategy.prepare_indicators(df) ONCE — Kalman walks prices[0..N-1]
        deterministically; this is the same code path the sidecar invokes per
        bar (sidecar re-runs prepare_indicators each bar over its 1500-bar
        window; effective semantics are identical because the Kalman recursion
        is purely a function of the input price sequence)
      * call apply_regime_model(df) ONCE — same pattern
      * walk evaluate_bar(df, i, state, strategy, config) for i in 0..N-1; the
        BarState advances bar-by-bar (positions, pending_entry, etc.); we
        record per-live-bar telemetry from the publicly-exposed columns
    """
    print(f"\n[{label}] warmup_hours={warmup_hours}")
    print(f"[{label}] live window: {live_start} -> {live_end}")

    sliced = slice_window(df_full, warmup_hours, live_start, live_end)
    print(f"[{label}] sliced bars: {len(sliced)}  "
          f"({sliced.index[0]} -> {sliced.index[-1]})")

    strategy = load_strategy()
    config   = resolve_engine_config(strategy)
    state    = BarState()

    # Public engine surfaces: prepare_indicators + apply_regime_model
    df_eval = strategy.prepare_indicators(sliced.copy())
    df_eval = apply_regime_model(df_eval)

    # Walk evaluate_bar across the entire window; capture telemetry only for
    # bars within the live window so warmup bars don't pollute output.
    n = len(df_eval)
    bar_records: list[dict[str, Any]] = []
    fired_bars: list[str] = []

    for i in range(n):
        prev_pending = state.pending_entry
        evaluate_bar(df_eval, i, state, strategy, config)
        bar_ts = df_eval.index[i]
        if bar_ts < live_start:
            continue  # warmup bar; advance state silently

        new_pending = state.pending_entry
        fired = (new_pending is not None) and (new_pending is not prev_pending)
        if fired:
            fired_bars.append(bar_ts.strftime("%Y-%m-%d %H:%M:%S"))

        signal_dict: dict[str, Any] | None = None
        if fired and isinstance(new_pending, dict):
            sig = new_pending.get("signal", {}) or {}
            signal_dict = {
                "signal": sig.get("signal"),
                "entry_reason": sig.get("entry_reason"),
                "entry_reference_price": _scalar_or_none(sig.get("entry_reference_price")),
            }

        rec: dict[str, Any] = {
            "label":          label,
            "warmup_hours":   warmup_hours,
            "bar_ts_utc":     bar_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "bar_idx":        i,
            "close":          _scalar_or_none(df_eval["close"].iloc[i]),
            "kalman_trend":   _scalar_or_none(df_eval["kalman_trend"].iloc[i])
                                if "kalman_trend" in df_eval.columns else None,
            "kalman_regime":  _scalar_or_none(df_eval["kalman_regime"].iloc[i])
                                if "kalman_regime" in df_eval.columns else None,
            "kalman_flip":    bool(df_eval["kalman_flip"].iloc[i])
                                if "kalman_flip" in df_eval.columns else None,
            "atr":            _scalar_or_none(df_eval["atr"].iloc[i])
                                if "atr" in df_eval.columns else None,
            "adx":            _scalar_or_none(df_eval["adx"].iloc[i])
                                if "adx" in df_eval.columns else None,
            "rsi_smoothed":   _scalar_or_none(df_eval["rsi_smoothed"].iloc[i])
                                if "rsi_smoothed" in df_eval.columns else None,
            "hurst":          _scalar_or_none(df_eval["hurst"].iloc[i])
                                if "hurst" in df_eval.columns else None,
            "in_pos":         bool(state.in_pos),
            "direction":      int(state.direction),
            "fired_this_bar": fired,
            "pending_entry":  signal_dict,
        }
        bar_records.append(rec)

    print(f"[{label}] live bars evaluated: {len(bar_records)}  "
          f"fires: {len(fired_bars)}")
    if fired_bars:
        print(f"[{label}] fired_bars: {fired_bars}")

    return {
        "label":               label,
        "warmup_hours":        warmup_hours,
        "live_bars_evaluated": len(bar_records),
        "fired_bars":          fired_bars,
        "all_records":         bar_records,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="H1/H4 investigation: deterministic Kalman replay "
                    "with controlled warmup windows.")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--data",    type=Path, default=DATA_CSV)
    p.add_argument("--warmup-a", type=float, default=72.0,
                   help="Replay A warmup hours (default 72)")
    p.add_argument("--warmup-b", type=float, default=12.0,
                   help="Replay B warmup hours (default 12)")
    args = p.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(f"Kalman replay harness — strategy {STRATEGY_ID}")
    print(f"v1.5.9 evaluate_bar source : {VAULT_SNAPSHOT}")
    print(f"Trade_Scan packages        : {TRADE_SCAN_ROOT}")
    print(f"Bar data                   : {args.data}")
    print(f"Output                     : {args.out_dir}")
    print(f"Live window                : {LIVE_WINDOW_START}  ->  {LIVE_WINDOW_END}")
    print(f"Replay A warmup            : {args.warmup_a} h")
    print(f"Replay B warmup            : {args.warmup_b} h")
    print("=" * 78)

    df_full = load_bars(args.data)
    print(f"loaded {len(df_full)} bars  {df_full.index.min()} -> {df_full.index.max()}")

    # Replay A
    rep_a = replay("REPLAY_A_72h", args.warmup_a, df_full,
                   LIVE_WINDOW_START, LIVE_WINDOW_END)
    # Replay B
    rep_b = replay("REPLAY_B_12h", args.warmup_b, df_full,
                   LIVE_WINDOW_START, LIVE_WINDOW_END)

    # Persist per-bar JSONLs
    for r in (rep_a, rep_b):
        out = args.out_dir / f"{r['label']}.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for rec in r["all_records"]:
                f.write(json.dumps(rec) + "\n")
        print(f"wrote {len(r['all_records'])} bars -> {out}")

    # Build summary keyed by target bars
    a_by_ts = {r["bar_ts_utc"]: r for r in rep_a["all_records"]}
    b_by_ts = {r["bar_ts_utc"]: r for r in rep_b["all_records"]}

    target_compare: list[dict[str, Any]] = []
    for ts in TARGET_BARS_UTC:
        a = a_by_ts.get(ts, {})
        b = b_by_ts.get(ts, {})
        kt_a = a.get("kalman_trend")
        kt_b = b.get("kalman_trend")
        delta = (kt_a - kt_b) if (kt_a is not None and kt_b is not None) else None
        target_compare.append({
            "bar_ts_utc":          ts,
            "live_reference":      LIVE_REFERENCE.get(ts),
            "a_fired":             a.get("fired_this_bar"),
            "b_fired":             b.get("fired_this_bar"),
            "a_kalman_regime":     a.get("kalman_regime"),
            "b_kalman_regime":     b.get("kalman_regime"),
            "a_kalman_flip":       a.get("kalman_flip"),
            "b_kalman_flip":       b.get("kalman_flip"),
            "a_kalman_trend":      kt_a,
            "b_kalman_trend":      kt_b,
            "kalman_trend_delta":  delta,
            "a_adx":               a.get("adx"),
            "b_adx":               b.get("adx"),
            "a_rsi_smoothed":      a.get("rsi_smoothed"),
            "b_rsi_smoothed":      b.get("rsi_smoothed"),
            "a_hurst":             a.get("hurst"),
            "b_hurst":             b.get("hurst"),
            "a_in_pos":            a.get("in_pos"),
            "b_in_pos":            b.get("in_pos"),
            "a_close":             a.get("close"),
            "b_close":             b.get("close"),
        })

    summary = {
        "strategy_id":          STRATEGY_ID,
        "live_window_start":    LIVE_WINDOW_START.strftime("%Y-%m-%d %H:%M:%S"),
        "live_window_end":      LIVE_WINDOW_END.strftime("%Y-%m-%d %H:%M:%S"),
        "warmup_a_hours":       args.warmup_a,
        "warmup_b_hours":       args.warmup_b,
        "replay_a_total_fires": len(rep_a["fired_bars"]),
        "replay_b_total_fires": len(rep_b["fired_bars"]),
        "replay_a_fired_bars":  rep_a["fired_bars"],
        "replay_b_fired_bars":  rep_b["fired_bars"],
        "target_bars":          target_compare,
        "engine_version":       "v1_5_9 (vault snapshot)",
        "evaluate_bar_module":  evaluate_bar.__module__,
    }
    summary_path = args.out_dir / "kalman_replay_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"wrote summary -> {summary_path}")

    # Console headline
    print()
    print(f"{'bar_ts_utc':<22} | {'A_72h':>6} | {'B_12h':>6} | "
          f"{'A_reg':>5} | {'B_reg':>5} | {'A_flip':>6} | {'B_flip':>6} | "
          f"{'live_engine':<10} | {'live_exec':<10}")
    print("-" * 110)
    for c in target_compare:
        ref = c["live_reference"] or {}
        print(f"{c['bar_ts_utc']:<22} | "
              f"{str(c['a_fired']):>6} | {str(c['b_fired']):>6} | "
              f"{str(c['a_kalman_regime']):>5} | {str(c['b_kalman_regime']):>5} | "
              f"{str(c['a_kalman_flip']):>6} | {str(c['b_kalman_flip']):>6} | "
              f"{ref.get('sidecar','?'):<10} | {ref.get('execution','?'):<10}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
