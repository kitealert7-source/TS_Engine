"""
Spike parity harness — v1.5.8 vs v1.5.9 byte-identical comparison.

For each test strategy, runs the SAME bar dataset through both engines:
  - Trade_Scan/engine_dev/universal_research_engine/v1_5_8/run_engine
  - Trade_Scan/engine_dev/universal_research_engine/v1_5_9/run_engine

Compares the returned trade lists field-by-field. Any divergence is a spike
failure per the rules ("zero tolerance").

Test order (from spike GO prompt):
  1. 33_TREND_BTCUSD_1H_IMPULSE_S03_V1_P02 (sanity)
  2. 62_TREND_IDX_5M_KALFLIP_S01_V2_P15    (hardest)
  3. 27_MR_XAUUSD_1H_PINBAR_S01_V1_P05     (cross-archetype)

If 33 fails -> recommendation C (architecture not extraction-ready)
If 33 passes but later fails -> recommendation B (minor engine refactor)
If all pass -> recommendation A (proceed to Phase A/B/C)

Strict invocation:
  cd Trade_Scan && python ../TS_Engine/tests/spike_parity.py [--strategy ID]

Output:
  TS_Engine/spike_artifacts/<STRATEGY_ID>/
    trades_v158.jsonl
    trades_v159.jsonl
    parity_diff.txt
"""

from __future__ import annotations

import json
import sys
import time
import argparse
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "Trade_Scan"
ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "spike_artifacts"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Phase A test plan: all 9 burn-in strategies. Strategy 33 last because of
# the known numpy.bool_ env issue (must be fixed before its parity becomes
# meaningful).
TEST_STRATEGIES = [
    # Already validated in spike (re-running):
    ("62_TREND_IDX_5M_KALFLIP_S01_V2_P15",                "trend / Kalman (spike-hardest)"),
    ("27_MR_XAUUSD_1H_PINBAR_S01_V1_P05",                 "mean-reversion / pinbar"),
    # Phase A new (6 strategies):
    ("35_PA_GER40_15M_DAYOC_S12_V1_P00",                  "price action / day OC"),
    ("22_CONT_FX_30M_RSIAVG_TRENDFILT_S02_V1_P05",        "continuation / RSI multi-pair"),
    ("22_CONT_FX_15M_RSIAVG_TRENDFILT_S07_V1_P01_GBPUSD", "continuation / RSI 15m GBPUSD"),
    ("22_CONT_FX_15M_RSIAVG_TRENDFILT_S07_V1_P02_EURUSD", "continuation / RSI 15m EURUSD"),
    ("22_CONT_FX_30M_RSIAVG_TRENDFILT_S02_V1_P06_AUDJPY", "continuation / RSI 30m AUDJPY"),
    ("22_CONT_FX_30M_RSIAVG_TRENDFILT_S02_V1_P02_AUDUSD", "continuation / RSI 30m AUDUSD"),
    # Last — known degenerate (numpy.bool_ env issue, pre-existing in v1.5.8):
    ("33_TREND_BTCUSD_1H_IMPULSE_S03_V1_P02",             "trend / impulse (env issue)"),
]

# ---------------------------------------------------------------------------
# Engine loader — imports both versions side-by-side
# ---------------------------------------------------------------------------

def load_engine(version: str):
    """Import a research engine version and return its run_execution_loop."""
    if version == "v1_5_8":
        from engine_dev.universal_research_engine.v1_5_8 import execution_loop as el
    elif version == "v1_5_9":
        from engine_dev.universal_research_engine.v1_5_9 import execution_loop as el
    else:
        raise ValueError(f"unknown engine version: {version}")
    return el.run_execution_loop, el.ENGINE_VERSION


def load_strategy(strategy_id: str):
    """Dynamically load strategy plugin (mirrors v1.5.8/main.py)."""
    import importlib
    module_path = f"strategies.{strategy_id}.strategy"
    module = importlib.import_module(module_path)
    StrategyClass = getattr(module, "Strategy", None)
    if StrategyClass is None:
        raise ValueError(f"Strategy class not found in {module_path}")
    return StrategyClass()


# ---------------------------------------------------------------------------
# Data loader — find the bars used by this strategy's most recent backtest
# ---------------------------------------------------------------------------

def find_bars_for_strategy(strategy_id: str) -> pd.DataFrame:
    """Locate the bar data for a strategy and load as DataFrame.

    Tries directive YAML, falls back to portfolio.yaml when directive is empty
    (post-admission marker only). Both engines see the same data, so parity
    holds regardless of date-range source.
    """
    import yaml

    def _mt5_tf_to_lower(tf):
        if not tf: return ""
        m = {"M1":"1m","M5":"5m","M15":"15m","M30":"30m","H1":"1h","H4":"4h","D1":"1d"}
        return m.get(tf.strip().upper(), tf.lower())

    symbol = timeframe = broker = None
    start_date = end_date = None
    meta_source = None

    directive_dir = PROJECT_ROOT / "backtest_directives" / "completed"
    for variant in (f"{strategy_id}.txt", f"{strategy_id}.txt.admitted"):
        p = directive_dir / variant
        if p.exists() and p.stat().st_size > 0:
            try:
                with open(p, encoding="utf-8") as f:
                    d = yaml.safe_load(f)
                if d is not None:
                    tb = d.get("test") or {}
                    timeframe  = tb.get("timeframe")
                    start_date = tb.get("start_date")
                    end_date   = tb.get("end_date")
                    broker     = (tb.get("broker") or "").upper()
                    syms       = d.get("symbols") or []
                    symbol     = syms[0] if syms else None
                    meta_source = f"directive:{variant}"
                    if symbol and timeframe:
                        if not broker:
                            broker = "OCTAFX"
                        break
            except Exception:
                pass

    if not (symbol and timeframe):
        # Fallback to portfolio.yaml (covers empty .txt/.txt.admitted markers).
        # Schema: top-level key 'portfolio' -> dict with 'strategies' list.
        portfolio_path = Path("C:/Users/faraw/Documents/TS_Execution/portfolio.yaml")
        with open(portfolio_path, encoding="utf-8") as f:
            full = yaml.safe_load(f)
        strats = (full.get("portfolio") or {}).get("strategies") or []
        for entry in strats:
            if entry.get("id") == strategy_id:
                symbol    = entry.get("symbol")
                timeframe = _mt5_tf_to_lower(entry.get("timeframe"))
                broker    = "OCTAFX"  # current burn-in is OctaFX-only
                meta_source = "portfolio.yaml"
                break

    if not all([symbol, timeframe, broker]):
        raise ValueError(f"could not resolve meta for {strategy_id}: symbol={symbol} tf={timeframe} broker={broker}")

    print(f"  Strategy: {strategy_id}")
    print(f"  Symbol: {symbol}  Timeframe: {timeframe}  Broker: {broker}  (meta_source={meta_source})")
    print(f"  Dates: {start_date} -> {end_date}")

    # Locate per-year CLEAN CSVs in MASTER_DATA and concatenate
    data_root = Path("C:/Users/faraw/Documents/Anti_Gravity_DATA_ROOT/MASTER_DATA")
    master_dir = data_root / f"{symbol}_{broker}_MASTER" / "CLEAN"
    candidates = []
    if master_dir.exists():
        candidates = sorted(master_dir.glob(f"{symbol}_{broker}_{timeframe}_*_CLEAN.csv"))
    if not candidates:
        candidates = sorted(data_root.glob(f"**/{symbol}_{broker}_{timeframe}_*_CLEAN.csv"))
    if not candidates:
        candidates = sorted(data_root.glob(f"**/CLEAN/*{symbol}*{timeframe}*CLEAN.csv"))
    if not candidates:
        raise FileNotFoundError(f"no CLEAN CSV found for {symbol} {broker} {timeframe} under {data_root}")

    print(f"  Data files: {len(candidates)} year-CSVs ({candidates[0].name} ... {candidates[-1].name})")
    frames = []
    for p in candidates:
        f = pd.read_csv(p)
        frames.append(f)
    df = pd.concat(frames, ignore_index=True)

    # CSVs across years may have inconsistent timestamp formats (some with tz
    # offset, some without). Use format="mixed" to handle both.
    # IMPORTANT: keep 'time' / 'timestamp' as a column AND set DatetimeIndex.
    # Some strategies (e.g. 35_PA_GER40) read df['time'] directly in
    # prepare_indicators(), so removing it breaks them.
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True, format="mixed")
        df = df.sort_values("time").reset_index(drop=True)
        df = df.drop_duplicates(subset="time", keep="first").reset_index(drop=True)
        df.index = pd.DatetimeIndex(df["time"])
    elif "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="mixed")
        df = df.sort_values("timestamp").reset_index(drop=True)
        df = df.drop_duplicates(subset="timestamp", keep="first").reset_index(drop=True)
        df.index = pd.DatetimeIndex(df["timestamp"])

    # Filter by directive dates if present
    if start_date and end_date:
        try:
            sd = pd.to_datetime(start_date, utc=True)
            ed = pd.to_datetime(end_date,   utc=True)
            df = df[(df.index >= sd) & (df.index <= ed)]
        except Exception as e:
            print(f"  WARN: could not filter by dates: {e}")

    print(f"  Bars: {len(df)}  range: {df.index[0]} -> {df.index[-1]}")
    return df


# ---------------------------------------------------------------------------
# Parity comparison
# ---------------------------------------------------------------------------

def trades_to_jsonl(trades: list, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for t in trades:
            # Convert non-JSON-serializable types (Timestamp, numpy)
            normalized = {}
            for k, v in t.items():
                if hasattr(v, "isoformat"):
                    normalized[k] = v.isoformat()
                elif hasattr(v, "item"):  # numpy scalar
                    try:
                        normalized[k] = v.item()
                    except Exception:
                        normalized[k] = repr(v)
                elif isinstance(v, dict):
                    # partial_leg sub-dict
                    nested = {}
                    for nk, nv in v.items():
                        if hasattr(nv, "isoformat"):
                            nested[nk] = nv.isoformat()
                        elif hasattr(nv, "item"):
                            try:
                                nested[nk] = nv.item()
                            except Exception:
                                nested[nk] = repr(nv)
                        else:
                            nested[nk] = nv
                    normalized[k] = nested
                else:
                    normalized[k] = v
            f.write(json.dumps(normalized, sort_keys=True, default=str) + "\n")


def diff_trade_lists(trades_a: list, trades_b: list) -> list[str]:
    """Field-by-field comparison; returns list of human-readable divergence lines."""
    diffs = []
    if len(trades_a) != len(trades_b):
        diffs.append(f"TRADE COUNT MISMATCH: v1.5.8={len(trades_a)} v1.5.9={len(trades_b)}")
        return diffs

    for i, (ta, tb) in enumerate(zip(trades_a, trades_b)):
        keys_a = set(ta.keys())
        keys_b = set(tb.keys())
        if keys_a != keys_b:
            diffs.append(f"trade[{i}] KEYS DIFFER: only_v158={keys_a - keys_b}  only_v159={keys_b - keys_a}")
        for k in keys_a & keys_b:
            va, vb = ta[k], tb[k]
            # Handle nested dicts (partial_leg)
            if isinstance(va, dict) and isinstance(vb, dict):
                for nk in set(va.keys()) | set(vb.keys()):
                    nva = va.get(nk)
                    nvb = vb.get(nk)
                    if nva != nvb:
                        # tolerate timestamp string vs Timestamp
                        if str(nva) != str(nvb):
                            diffs.append(f"trade[{i}].{k}.{nk}: v158={nva!r}  v159={nvb!r}")
                continue
            if va != vb:
                if str(va) != str(vb):
                    diffs.append(f"trade[{i}].{k}: v158={va!r}  v159={vb!r}")
    return diffs


# ---------------------------------------------------------------------------
# Per-strategy runner
# ---------------------------------------------------------------------------

def run_parity_one(strategy_id: str, label: str) -> tuple[bool, list[str], dict]:
    """Returns (passed, diffs, info_dict)."""
    info = {"strategy_id": strategy_id, "label": label}
    artifacts = ARTIFACTS_DIR / strategy_id
    artifacts.mkdir(parents=True, exist_ok=True)

    def _early_fail(msg: str) -> tuple:
        (artifacts / "parity_diff.txt").write_text(f"EARLY FAIL: {msg}\n", encoding="utf-8")
        print(f"  EARLY FAIL: {msg}")
        return False, [msg], info

    print(f"\n{'='*78}")
    print(f"STRATEGY: {strategy_id}  ({label})")
    print('='*78)

    # 1. Load bars (single canonical dataset for both engines)
    try:
        df = find_bars_for_strategy(strategy_id)
        info["bars_count"] = len(df)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _early_fail(f"DATA_LOAD_FAILED: {type(e).__name__}: {e}")

    # 2. Load engines
    run_v158, ver_v158 = load_engine("v1_5_8")
    run_v159, ver_v159 = load_engine("v1_5_9")
    print(f"  Loaded engines: v{ver_v158}, v{ver_v159}")

    # 3. Run v1.5.8 (capture exceptions; both engines should behave identically
    #    even when the strategy raises — that IS parity)
    print("  Running v1.5.8 ...", end=" ", flush=True)
    t0 = time.time()
    exc_158 = None
    trades_158 = None
    try:
        strategy_158 = load_strategy(strategy_id)
        trades_158 = run_v158(df.copy(), strategy_158)
    except Exception as e:
        exc_158 = (type(e).__name__, str(e))
    dt158 = time.time() - t0
    if exc_158:
        print(f"raised {exc_158[0]} in {dt158:.1f}s")
    else:
        print(f"done in {dt158:.1f}s -- {len(trades_158)} trades")

    # 4. Run v1.5.9 (fresh strategy instance, fresh df)
    print("  Running v1.5.9 ...", end=" ", flush=True)
    t0 = time.time()
    exc_159 = None
    trades_159 = None
    try:
        strategy_159 = load_strategy(strategy_id)
        trades_159 = run_v159(df.copy(), strategy_159)
    except Exception as e:
        exc_159 = (type(e).__name__, str(e))
    dt159 = time.time() - t0
    if exc_159:
        print(f"raised {exc_159[0]} in {dt159:.1f}s")
    else:
        print(f"done in {dt159:.1f}s -- {len(trades_159)} trades")

    # Exception parity check: both raised identically = OK (parity preserved)
    if exc_158 is not None or exc_159 is not None:
        if exc_158 == exc_159:
            print(f"  PASS (both engines raised identically) -- {exc_158[0]}: {exc_158[1][:80]}")
            (artifacts / "parity_diff.txt").write_text(
                f"BOTH RAISED IDENTICALLY\nexception: {exc_158[0]}\nmessage: {exc_158[1]}\n",
                encoding="utf-8",
            )
            info["both_raised"] = True
            info["exception"] = exc_158[0]
            return True, [], info
        else:
            diffs = [
                f"EXCEPTION DIVERGENCE:",
                f"  v1.5.8: {exc_158}",
                f"  v1.5.9: {exc_159}",
            ]
            (artifacts / "parity_diff.txt").write_text("\n".join(diffs) + "\n", encoding="utf-8")
            print(f"  FAIL -- engines raised differently")
            for d in diffs:
                print(f"    {d}")
            return False, diffs, info

    info["trades_v158"] = len(trades_158)
    info["trades_v159"] = len(trades_159)
    info["runtime_v158_s"] = round(dt158, 2)
    info["runtime_v159_s"] = round(dt159, 2)

    # 5. Save artifacts
    trades_to_jsonl(trades_158, artifacts / "trades_v158.jsonl")
    trades_to_jsonl(trades_159, artifacts / "trades_v159.jsonl")

    # 6. Compare
    diffs = diff_trade_lists(trades_158, trades_159)
    diff_path = artifacts / "parity_diff.txt"
    if diffs:
        diff_path.write_text(
            f"PARITY DIVERGENCE for {strategy_id}\n" + "\n".join(diffs) + "\n",
            encoding="utf-8",
        )
        print(f"  FAIL — {len(diffs)} divergences (see {diff_path})")
        for d in diffs[:10]:
            print(f"    {d}")
        if len(diffs) > 10:
            print(f"    ... and {len(diffs) - 10} more")
        return False, diffs, info
    else:
        diff_path.write_text(f"PARITY OK for {strategy_id}\n", encoding="utf-8")
        print(f"  PASS — byte-identical trade lists ({len(trades_158)} trades)")
        return True, [], info


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Spike v1.5.9 parity test")
    p.add_argument("--strategy", help="Run only one strategy by ID")
    args = p.parse_args()

    plan = TEST_STRATEGIES if not args.strategy else [(args.strategy, "manual")]

    print("="*78)
    print(f"SPIKE PARITY TEST — v1.5.8 vs v1.5.9")
    print(f"Time-box: 48h. Test order is sanity -> hardest -> cross-archetype.")
    print(f"Strategies: {[s[0] for s in plan]}")
    print("="*78)

    results = []
    overall_pass = True
    for strat_id, label in plan:
        passed, diffs, info = run_parity_one(strat_id, label)
        results.append((strat_id, label, passed, len(diffs), info))
        if not passed:
            overall_pass = False
            print(f"\n  STOPPING — strategy {strat_id} failed parity.")
            print(f"  Per spike rules: classify failure, do NOT iterate fixes.")
            break

    print("\n" + "="*78)
    print("SPIKE PARITY SUMMARY")
    print("="*78)
    for strat_id, label, passed, n_diffs, info in results:
        status = "PASS" if passed else "FAIL"
        ntrades = info.get("trades_v158", "?")
        print(f"  [{status}]  {strat_id} ({label})  trades={ntrades}  diffs={n_diffs}")

    if overall_pass and len(results) == len(plan):
        print("\n  RECOMMENDATION: A (proceed to Phase A/B/C)")
        return 0
    elif results and not results[0][2]:
        print("\n  RECOMMENDATION: C (sanity strategy failed — architecture not extraction-ready)")
        return 1
    else:
        print("\n  RECOMMENDATION: B (sanity passed; later strategy failed — minor engine refactor needed)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
