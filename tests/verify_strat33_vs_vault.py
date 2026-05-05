"""
Verify that the bool() cast fix to Strategy 33 preserves v1.5.8 behavior.

Procedure:
  1. Run patched Strategy 33 through v1.5.8 (unmodified) on the canonical
     directive date range (2020-01-04 to 2026-04-15).
  2. Compare resulting trade list against vault snapshot's
     results_tradelevel.csv (DRY_RUN_2026_04_06__71723056 — the canonical
     output from when this strategy was originally backtested + admitted).
  3. Acceptance: every trade row in the vault must be reproduced byte-equivalent
     by the patched strategy + v1.5.8. Field set differs (vault has stage2
     additions like pnl_usd, r_multiple); compare on engine-output field
     subset only.

If trades match: bool() cast preserves v1.5.8 behavior (no silent coercion;
proven by output identity to canonical ledger).
"""
from __future__ import annotations

import sys
import csv
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "Trade_Scan"
sys.path.insert(0, str(PROJECT_ROOT))

VAULT_PATH = (Path("C:/Users/faraw/Documents/DRY_RUN_VAULT") /
              "DRY_RUN_2026_04_06__71723056" /
              "33_TREND_BTCUSD_1H_IMPULSE_S03_V1_P02" /
              "run_snapshot" / "data" / "results_tradelevel.csv")

# Fields that the engine produces directly (subset of vault fields)
# These are what we can compare apples-to-apples
ENGINE_OUTPUT_FIELDS = [
    "entry_timestamp",
    "exit_timestamp",
    "direction",
    "entry_price",
    "exit_price",
    "bars_held",
    "trade_high",
    "trade_low",
    "atr_entry",
    "initial_stop_price",
    "risk_distance",
    "volatility_regime",
    "trend_score",
    "trend_regime",
    "trend_label",
]


def load_vault_trades() -> list[dict]:
    """Load vault snapshot's canonical trade ledger."""
    rows = []
    with open(VAULT_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def load_test_data() -> pd.DataFrame:
    """Load BTCUSD 1h bars from VAULT date range (2024-01-04 to 2026-04-03).
    Keep 'time' as column AND set DatetimeIndex (engine emits entry_timestamp
    via df.iloc[i].get('time'), so 'time' must remain a column)."""
    data_root = Path("C:/Users/faraw/Documents/Anti_Gravity_DATA_ROOT/MASTER_DATA")
    files = sorted((data_root / "BTCUSD_OCTAFX_MASTER" / "CLEAN").glob(
        "BTCUSD_OCTAFX_1h_*_CLEAN.csv"))
    frames = [pd.read_csv(p) for p in files]
    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["time"], utc=True, format="mixed")
    df = df.sort_values("time").reset_index(drop=True)
    df = df.drop_duplicates(subset="time", keep="first").reset_index(drop=True)
    # Set index BUT keep 'time' as a column so engine can read it via .get('time')
    df.index = pd.DatetimeIndex(df["time"])
    # Filter to vault's actual date range (where canonical 262-trade ledger came from)
    sd = pd.to_datetime("2024-01-04", utc=True)
    ed = pd.to_datetime("2026-04-03 23:59:59", utc=True)
    df = df[(df.index >= sd) & (df.index <= ed)]
    return df


def run_v158_patched() -> list[dict]:
    """Run the patched strategy through v1.5.8 (unmodified)."""
    from engine_dev.universal_research_engine.v1_5_8 import execution_loop as el
    import importlib
    m = importlib.import_module(
        "strategies.33_TREND_BTCUSD_1H_IMPULSE_S03_V1_P02.strategy")
    strategy = m.Strategy()
    df = load_test_data()
    print(f"Loaded {len(df)} bars, range {df.index[0]} -> {df.index[-1]}")
    trades = el.run_execution_loop(df, strategy)
    return trades


def normalize_value(v):
    """Normalize for cross-source comparison (CSV strings vs Python types)."""
    if v is None or v == "":
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, str):
        s = v.strip()
        # Try numeric
        try:
            return float(s)
        except (ValueError, TypeError):
            return s
    if isinstance(v, (int, float)):
        return float(v)
    return v


def compare(trades_engine: list[dict], trades_vault: list[dict]) -> tuple[int, list[str]]:
    """Field-by-field compare on ENGINE_OUTPUT_FIELDS. Returns (n_differences, lines)."""
    diffs = []
    if len(trades_engine) != len(trades_vault):
        diffs.append(f"COUNT MISMATCH: engine={len(trades_engine)}  vault={len(trades_vault)}")

    n = min(len(trades_engine), len(trades_vault))
    for i in range(n):
        te = trades_engine[i]
        tv = trades_vault[i]
        for field in ENGINE_OUTPUT_FIELDS:
            ev = normalize_value(te.get(field))
            vv = normalize_value(tv.get(field))
            # Float tolerance: trades from CSV → string round-trip, so strict
            # equality may fail on floats due to representation. Use small eps.
            if isinstance(ev, float) and isinstance(vv, float):
                if abs(ev - vv) > 1e-6:
                    diffs.append(f"trade[{i}].{field}: engine={ev!r} vault={vv!r}")
            else:
                if ev != vv:
                    diffs.append(f"trade[{i}].{field}: engine={ev!r} vault={vv!r}")
    return len(diffs), diffs


def main() -> int:
    print("=" * 78)
    print("VERIFY: patched Strategy 33 + v1.5.8 vs vault canonical ledger")
    print("=" * 78)

    print("\nLoading vault canonical ledger...")
    vault_trades = load_vault_trades()
    print(f"  vault: {len(vault_trades)} trades")

    print("\nRunning patched Strategy 33 through v1.5.8...")
    engine_trades = run_v158_patched()
    print(f"  engine: {len(engine_trades)} trades")

    print("\nComparing on engine-output fields...")
    n_diffs, diffs = compare(engine_trades, vault_trades)

    if n_diffs == 0:
        print(f"\n  PASS — patched strategy + v1.5.8 produces {len(engine_trades)} trades")
        print(f"         identical (within float eps 1e-6) to vault canonical ledger.")
        print(f"         bool() cast preserves v1.5.8 behavior. PROOF DEMONSTRATED.")
        return 0
    else:
        print(f"\n  FAIL — {n_diffs} divergences from vault ledger:")
        for d in diffs[:20]:
            print(f"    {d}")
        if n_diffs > 20:
            print(f"    ... and {n_diffs - 20} more")
        return 1


if __name__ == "__main__":
    sys.exit(main())
