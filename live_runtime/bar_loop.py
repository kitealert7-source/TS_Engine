"""
bar_loop.py — per-(symbol, timeframe) bar callback loop for TS_Engine sidecar.

Each loop:
  1. Sleeps until ~20 seconds after expected bar close (lets TS_Execution
     poll first; eliminates race with TS_Execution's bar detection).
  2. Fetches the latest N bars from MT5 (read-only).
  3. Runs apply_regime_model(df) and per-strategy prepare_indicators(df).
  4. Calls v1.5.9 evaluate_bar(df, latest_idx, state, strategy, config).
  5. Inspects state.pending_entry — if a new entry fired this bar, writes
     a shadow signal record.
  6. Sleeps to the next bar close.

State is held in memory across bars. On startup, replays a warm-up window
to bring state in line with current market state before live evaluation
begins.

Threading model: one thread per (symbol, tf) group. Threads share the MT5
connection (MT5 Python is thread-safe for read calls within one process).
"""

from __future__ import annotations

import sys
import time
import traceback
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# Engine imports — v1.5.9 evaluate_bar (the shared function)
TRADE_SCAN_ROOT = Path(__file__).resolve().parents[2] / "Trade_Scan"
# v1.5.9 currently lives only inside the DR_BASELINE_2026_05_03_v1_5_8a vault
# snapshot. Path setup must put the snapshot ahead of TRADE_SCAN_ROOT so
# `engine_dev.universal_research_engine.v1_5_9` resolves while `engines`,
# `strategies`, `indicators` continue to come from TRADE_SCAN_ROOT itself.
# Telemetry-only addition — no logic or signal-path change.
_VAULT_V1_5_9_ROOT = (
    TRADE_SCAN_ROOT / "vault" / "snapshots" / "DR_BASELINE_2026_05_03_v1_5_8a"
)
for _p in (TRADE_SCAN_ROOT, _VAULT_V1_5_9_ROOT):
    _sp = str(_p)
    if _sp in sys.path:
        sys.path.remove(_sp)
    sys.path.insert(0, _sp)

from engine_dev.universal_research_engine.v1_5_9.evaluate_bar import (  # noqa: E402
    BarState,
    EngineConfig,
    evaluate_bar,
    resolve_engine_config,
)
from engines.regime_state_machine import apply_regime_model  # noqa: E402

from . import mt5_reader  # local
from .shadow_journal import ShadowJournal, BarTelemetryJournal, ExitSignalJournal  # local
# OBSERVABILITY_CANONICAL_HASH_V1
from .observability_hash import ohlc_sha256, CANONICAL_HASH_VERSION  # local


# ---------------------------------------------------------------------------
# Group config
# ---------------------------------------------------------------------------

@dataclass
class GroupConfig:
    symbol:    str
    timeframe: str          # lowercase "5m", "1h", etc.
    period_s:  int          # 300 for 5m, 3600 for 1h
    strategies: list[Any]   # list of strategy instances
    strategy_ids: list[str]


@dataclass
class StrategyState:
    """Per-strategy mutable state held across bar evaluations."""
    strategy_id: str
    strategy:    Any
    bar_state:   BarState
    config:      EngineConfig
    last_pending_bar_idx: int = -1  # to detect new pending_entry vs. carry-over


# Bar window size — must be >= max indicator warmup across all strategies.
# 1500 bars is comfortable for: ATR(14), Hurst(100), regime model (~250),
# Kalman warmup. Adjust per-group if needed.
WARMUP_BAR_COUNT = 1500
LIVE_FETCH_COUNT = 1500
POST_CLOSE_DELAY_S = 20  # let TS_Execution poll first


# ---------------------------------------------------------------------------
# Bar window construction
# ---------------------------------------------------------------------------

def _rates_to_df(rates) -> pd.DataFrame:
    """MT5 rates structured array → pandas DataFrame in canonical shape."""
    df = pd.DataFrame(rates)
    if "time" in df.columns:
        # MT5 returns 'time' as unix seconds (UTC)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.index = pd.DatetimeIndex(df["time"])
    return df


def _fetch_window(symbol: str, tf_lower: str, n_bars: int) -> pd.DataFrame:
    """Read-only fetch of last n_bars completed+forming bars."""
    rates = mt5_reader.copy_rates(symbol, tf_lower, n_bars)
    if rates is None or len(rates) == 0:
        raise RuntimeError(
            f"copy_rates returned empty for {symbol} {tf_lower}: "
            f"err={mt5_reader.last_error()}"
        )
    return _rates_to_df(rates)


# ---------------------------------------------------------------------------
# Engine evaluation per strategy
# ---------------------------------------------------------------------------

def _build_warmed_state(df: pd.DataFrame, strategy, config: EngineConfig
                       ) -> tuple[BarState, int]:
    """Replay through historical bars to bring BarState up to current.

    Returns (state, last_processed_bar_idx). The last processed bar is the
    last CLOSED bar in df (df.iloc[-2]). df.iloc[-1] is the forming bar.
    """
    state = BarState()
    df_local = strategy.prepare_indicators(df.copy())
    df_local = apply_regime_model(df_local)
    # Iterate up to AND including the last closed bar (-2 in canonical
    # rates layout). df.iloc[-1] is the forming bar — never evaluated.
    last_closed = len(df_local) - 2
    if last_closed < 0:
        return state, -1
    for i in range(0, last_closed + 1):
        evaluate_bar(df_local, i, state, strategy, config)
    return state, last_closed


def _evaluate_one_new_bar(
    df: pd.DataFrame,
    latest_closed_idx: int,
    state: BarState,
    strategy,
    config: EngineConfig,
) -> dict | None:
    """Evaluate ONE new closed bar at index latest_closed_idx.

    df has indicators applied + regime model applied (call from outer scope).
    Returns the trade dict if a trade completed this bar, else None.
    Mutates state in place (pending_entry, in_pos, etc.).
    """
    return evaluate_bar(df, latest_closed_idx, state, strategy, config)


# ---------------------------------------------------------------------------
# Bar-close timing
# ---------------------------------------------------------------------------

def _seconds_until_next_close(period_s: int, now: datetime | None = None) -> float:
    """Seconds from now to next bar-close instant for this period."""
    now = now or datetime.now(timezone.utc)
    epoch = now.replace(tzinfo=timezone.utc).timestamp()
    next_close = (int(epoch) // period_s + 1) * period_s
    return next_close - epoch


def _format_bar_ts_for_journal(ts: pd.Timestamp) -> str:
    """Match TS_Execution's bar_ts format: '2026-05-01 03:15 UTC'."""
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def run_group_loop(
    group: GroupConfig,
    journal: ShadowJournal,
    stop_flag: threading.Event,
    bar_telemetry: BarTelemetryJournal | None = None,
    exit_journal: ExitSignalJournal | None = None,
) -> None:
    """Main bar-close loop for one (symbol, tf) group. Runs until stop_flag set.

    bar_telemetry / exit_journal are optional telemetry-only writers. When
    provided, every bar evaluation emits a BAR_TELEMETRY record, and any
    in_pos True->False transition emits an EXIT_SIGNAL record. Neither
    affects the per-bar engine logic, the BarState mutations, or the
    SHADOW_SIGNAL emission below.
    """
    sym, tf, period_s = group.symbol, group.timeframe, group.period_s
    log_prefix = f"[{sym} {tf}]"

    # OBSERVABILITY_CANONICAL_HASH_V1
    # Lazy-init telemetry journals if not supplied by caller. This lets the
    # existing runner.py invoke run_group_loop unchanged (callers that pass
    # explicit writers retain control). Both writers persist next to the
    # ShadowJournal's path so journal/ stays the canonical artifact dir.
    if bar_telemetry is None:
        try:
            _tel_path = journal._path.parent / "bar_telemetry.jsonl"  # type: ignore[attr-defined]
            bar_telemetry = BarTelemetryJournal(_tel_path, run_id=journal._run_id)  # type: ignore[attr-defined]
        except Exception as _tel_e:
            print(f"{log_prefix} BAR_TELEMETRY_INIT_FAILED: "
                  f"{type(_tel_e).__name__}: {_tel_e}")
            bar_telemetry = None
    if exit_journal is None:
        try:
            _exit_path = journal._path.parent / "exit_signal_journal.jsonl"  # type: ignore[attr-defined]
            exit_journal = ExitSignalJournal(_exit_path, run_id=journal._run_id)  # type: ignore[attr-defined]
        except Exception as _ej_e:
            print(f"{log_prefix} EXIT_JOURNAL_INIT_FAILED: "
                  f"{type(_ej_e).__name__}: {_ej_e}")
            exit_journal = None

    # 1. WARM UP: build initial state for each strategy
    print(f"{log_prefix} warming up state from {WARMUP_BAR_COUNT} bars...")
    try:
        df_warm = _fetch_window(sym, tf, WARMUP_BAR_COUNT)
    except Exception as e:
        print(f"{log_prefix} WARMUP_FETCH_FAILED: {type(e).__name__}: {e}")
        return

    states: list[StrategyState] = []
    for strategy in group.strategies:
        try:
            config = resolve_engine_config(strategy)
            state, last_idx = _build_warmed_state(df_warm, strategy, config)
            states.append(StrategyState(
                strategy_id=strategy.name,
                strategy=strategy,
                bar_state=state,
                config=config,
                last_pending_bar_idx=last_idx,
            ))
            print(f"{log_prefix}   warmed: {strategy.name}  "
                  f"in_pos={state.in_pos}  pending={'yes' if state.pending_entry else 'no'}")
        except Exception as e:
            print(f"{log_prefix}   WARMUP_FAILED: {strategy.name}: "
                  f"{type(e).__name__}: {e}")
            traceback.print_exc()

    if not states:
        print(f"{log_prefix} no strategies warmed; exiting loop")
        return

    # 2. LIVE LOOP
    journal.write_marker("GROUP_LIVE", detail=f"{sym}/{tf}")
    print(f"{log_prefix} entering live loop (period={period_s}s, "
          f"post-close delay={POST_CLOSE_DELAY_S}s)")

    while not stop_flag.is_set():
        # Sleep until POST_CLOSE_DELAY_S after the next bar close
        until_close = _seconds_until_next_close(period_s)
        sleep_s = until_close + POST_CLOSE_DELAY_S
        # Wait in small slices so stop_flag is responsive
        slept = 0.0
        while slept < sleep_s and not stop_flag.is_set():
            chunk = min(2.0, sleep_s - slept)
            time.sleep(chunk)
            slept += chunk
        if stop_flag.is_set():
            break

        # Fetch latest bars
        try:
            df = _fetch_window(sym, tf, LIVE_FETCH_COUNT)
        except Exception as e:
            print(f"{log_prefix} FETCH_FAILED: {type(e).__name__}: {e}")
            continue

        if len(df) < 2:
            print(f"{log_prefix} insufficient bars ({len(df)})")
            continue

        latest_closed_idx = len(df) - 2
        bar_ts_pd = df.index[latest_closed_idx]
        bar_ts_str = _format_bar_ts_for_journal(bar_ts_pd)

        # Per-strategy evaluation
        for ss in states:
            try:
                # Apply indicators + regime fresh each bar (matches TS_Execution)
                df_eval = ss.strategy.prepare_indicators(df.copy())
                df_eval = apply_regime_model(df_eval)

                # OBSERVABILITY_CANONICAL_HASH_V1 — input snapshot
                _telem_input: dict[str, Any] = {}
                _ohlc_hash: str | None = None
                if bar_telemetry is not None or exit_journal is not None:
                    try:
                        _ohlc_hash = ohlc_sha256(df_eval)
                    except Exception as _hash_e:
                        print(f"{log_prefix}   TELEMETRY_HASH_ERROR  "
                              f"{ss.strategy_id}: {type(_hash_e).__name__}: {_hash_e}")
                    _telem_input = {
                        "df_len":             int(len(df_eval)),
                        "first_bar_ts":       _format_bar_ts_for_journal(df_eval.index[0]),
                        "last_closed_bar_ts": _format_bar_ts_for_journal(
                            df_eval.index[latest_closed_idx]),
                        "forming_bar_ts":     _format_bar_ts_for_journal(df_eval.index[-1]),
                        "ohlc_sha256":        _ohlc_hash,
                        "hash_version":       CANONICAL_HASH_VERSION,
                    }

                # Snapshot pending_entry BEFORE evaluating this bar
                # — used to detect a NEWLY emitted signal vs carry-over
                pending_before = ss.bar_state.pending_entry

                # OBSERVABILITY_CANONICAL_HASH_V1 — decision pre-snapshot
                _bs = ss.bar_state
                _in_pos_before        = bool(_bs.in_pos)
                _direction_before     = int(_bs.direction)
                _entry_index_before   = int(_bs.entry_index)
                _stop_price_before    = (float(_bs.stop_price_active)
                                         if _bs.stop_price_active is not None else None)

                evaluate_bar(df_eval, latest_closed_idx, ss.bar_state,
                             ss.strategy, ss.config)

                # OBSERVABILITY_CANONICAL_HASH_V1 — decision post-snapshot
                _in_pos_after    = bool(ss.bar_state.in_pos)
                _direction_after = int(ss.bar_state.direction)

                # Emit BAR_TELEMETRY (one per evaluation, fire or no-fire)
                if bar_telemetry is not None:
                    _bars_in_position = (
                        latest_closed_idx - _entry_index_before
                        if _in_pos_before else None
                    )
                    _kalman_regime = (
                        _scalar(df_eval["kalman_regime"].iloc[latest_closed_idx])
                        if "kalman_regime" in df_eval.columns else None
                    )
                    _kalman_flip = (
                        bool(df_eval["kalman_flip"].iloc[latest_closed_idx])
                        if "kalman_flip" in df_eval.columns else None
                    )
                    _kalman_trend = (
                        _scalar(df_eval["kalman_trend"].iloc[latest_closed_idx])
                        if "kalman_trend" in df_eval.columns else None
                    )
                    _last_bar_for_regime = df_eval.iloc[latest_closed_idx]
                    bar_telemetry.write(
                        strategy_id        = ss.strategy_id,
                        symbol             = sym,
                        timeframe          = tf,
                        bar_ts             = bar_ts_str,
                        bar_idx            = int(latest_closed_idx),
                        # input snapshot
                        **_telem_input,
                        # decision snapshot
                        in_pos_before      = _in_pos_before,
                        in_pos_after       = _in_pos_after,
                        direction_before   = _direction_before,
                        direction_after    = _direction_after,
                        entry_index_before = _entry_index_before,
                        bars_in_position   = _bars_in_position,
                        entry_signal_fired = (ss.bar_state.pending_entry is not None
                                              and ss.bar_state.pending_entry is not pending_before),
                        exit_signal_fired  = (_in_pos_before and not _in_pos_after),
                        stop_price_active_before = _stop_price_before,
                        # regime / indicator snapshot
                        kalman_regime      = _kalman_regime,
                        kalman_flip        = _kalman_flip,
                        kalman_trend       = _kalman_trend,
                        regime_market      = _scalar(_last_bar_for_regime.get("market_regime")),
                        regime_trend       = _scalar(_last_bar_for_regime.get("trend_regime")),
                        regime_volatility  = _scalar(_last_bar_for_regime.get("volatility_regime")),
                        regime_id          = _scalar(_last_bar_for_regime.get("regime_id")),
                        regime_trend_label = _scalar(_last_bar_for_regime.get("trend_label")),
                    )

                # Emit EXIT_SIGNAL on in_pos transition True -> False
                if exit_journal is not None and _in_pos_before and not _in_pos_after:
                    _last_bar_x = df_eval.iloc[latest_closed_idx]
                    _bar_high   = float(_last_bar_x["high"])
                    _bar_low    = float(_last_bar_x["low"])
                    if _stop_price_before is not None and _direction_before == 1 \
                            and _bar_low <= _stop_price_before:
                        _exit_reason = "SL_INTRABAR_LONG"
                    elif _stop_price_before is not None and _direction_before == -1 \
                            and _bar_high >= _stop_price_before:
                        _exit_reason = "SL_INTRABAR_SHORT"
                    else:
                        _exit_reason = "STRATEGY_SIGNAL_EXIT"
                    _entry_bar_ts = (
                        _format_bar_ts_for_journal(df_eval.index[_entry_index_before])
                        if 0 <= _entry_index_before < len(df_eval)
                        else None
                    )
                    exit_journal.write(
                        strategy_id        = ss.strategy_id,
                        symbol             = sym,
                        timeframe          = tf,
                        bar_ts             = bar_ts_str,
                        direction          = _direction_before,
                        entry_index        = _entry_index_before,
                        entry_bar_ts       = _entry_bar_ts,
                        bars_in_position   = latest_closed_idx - _entry_index_before,
                        exit_reason_proxy  = _exit_reason,
                        stop_price_active  = _stop_price_before,
                        bar_high           = _bar_high,
                        bar_low            = _bar_low,
                        bar_close          = float(_last_bar_x["close"]),
                        kalman_regime      = (_scalar(df_eval["kalman_regime"].iloc[latest_closed_idx])
                                              if "kalman_regime" in df_eval.columns else None),
                        ohlc_sha256        = _ohlc_hash,
                        hash_version       = CANONICAL_HASH_VERSION,
                    )

                # If pending_entry transitioned from None to set, a signal
                # fired this bar. Write to shadow journal.
                pending_after = ss.bar_state.pending_entry
                if pending_after is not None and pending_after is not pending_before:
                    pe_signal = pending_after.get("signal", {})
                    sig_dir = pe_signal.get("signal")
                    entry_ref = pe_signal.get("entry_reference_price")
                    stop_price = pe_signal.get("stop_price")
                    reason = pe_signal.get("entry_reason", "")
                    tp = pe_signal.get("tp_price")

                    # Compute ENGINE_FALLBACK stop if strategy didn't emit one
                    if stop_price is None:
                        atr_v = pending_after.get("atr")
                        if atr_v and atr_v > 0:
                            mult = ss.config.sl_atr_mult
                            entry_ref_f = float(entry_ref) if entry_ref else 0.0
                            if sig_dir == 1:
                                stop_price = entry_ref_f - sig_dir * mult * atr_v
                            else:
                                stop_price = entry_ref_f - sig_dir * mult * atr_v

                    # Build bar_context similar to TS_Execution's _bar_context
                    last_bar = df_eval.iloc[-2]
                    bar_ctx = {
                        "volatility_regime": _scalar(last_bar.get("volatility_regime")),
                        "trend_regime":      _scalar(last_bar.get("trend_regime")),
                        "trend_label":       _scalar(last_bar.get("trend_label")),
                        "market_regime":     _scalar(last_bar.get("market_regime")),
                        "bar_high":          float(last_bar["high"]),
                        "bar_low":           float(last_bar["low"]),
                        "bar_close":         float(last_bar["close"]),
                    }

                    journal.write_signal(
                        strategy_id=ss.strategy_id,
                        symbol=sym,
                        bar_ts=bar_ts_str,
                        signal=int(sig_dir),
                        entry_reference_price=float(entry_ref) if entry_ref else 0.0,
                        stop_price=float(stop_price) if stop_price else 0.0,
                        entry_reason=str(reason),
                        tp_price=float(tp) if tp is not None else None,
                        bar_context=bar_ctx,
                    )
                    print(f"{log_prefix}   SHADOW_SIGNAL  {ss.strategy_id}  "
                          f"{bar_ts_str}  dir={sig_dir}  ref={entry_ref}")

            except Exception as e:
                print(f"{log_prefix} EVAL_ERROR {ss.strategy_id}: "
                      f"{type(e).__name__}: {e}")
                traceback.print_exc()

    journal.write_marker("GROUP_STOP", detail=f"{sym}/{tf}")
    print(f"{log_prefix} loop stopped")


def _scalar(v):
    """Convert numpy/pandas scalar to native Python type for JSON."""
    if v is None:
        return None
    try:
        import numpy as np
        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            return float(v)
    except ImportError:
        pass
    return v
