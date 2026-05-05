"""
shadow_journal.py — TS_Engine's signal output.

Mirrors the field schema of TS_Execution/journal/SignalJournal.jsonl so the
parity_monitor can align records by (strategy_id, bar_ts) and field-by-field
compare without translation overhead.

Append-only, fsync'd. Same crash-safety contract as TS_Execution's writer.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _signal_hash(strategy_id: str, bar_ts: str, signal: int,
                 entry_ref_price: float, stop_price: float) -> str:
    """Same hash recipe TS_Execution uses (signal_journal.signal_hash)."""
    raw = f"{strategy_id}|{bar_ts}|{signal}|{entry_ref_price:.6f}|{stop_price:.6f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ShadowJournal:
    """Writes shadow signals — emitted by TS_Engine's evaluate_bar replay.

    Field set is a SUBSET of TS_Execution's SignalJournal.jsonl entries —
    only the fields the parity_monitor needs to compare. We omit
    execution-quality timing fields (bar_detection_lag_s etc.) because
    TS_Engine doesn't dispatch and those fields aren't meaningful here.
    """

    def __init__(self, path: Path, run_id: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id

    def write_signal(
        self,
        *,
        strategy_id: str,
        symbol: str,
        bar_ts: str,
        signal: int,
        entry_reference_price: float,
        stop_price: float,
        entry_reason: str,
        tp_price: float | None = None,
        bar_context: dict[str, Any] | None = None,
    ) -> None:
        """Write a shadow signal record."""
        sig_hash = _signal_hash(strategy_id, bar_ts, signal,
                                entry_reference_price, stop_price)
        rec: dict[str, Any] = {
            "run_id":                self._run_id,
            "written_utc":           _dt.datetime.utcnow().isoformat(timespec="seconds"),
            "signal_hash":           sig_hash,
            "strategy_id":           strategy_id,
            "symbol":                symbol,
            "bar_ts":                bar_ts,
            "signal":                int(signal),
            "entry_reference_price": float(entry_reference_price),
            "stop_price":            float(stop_price),
            "entry_reason":          entry_reason,
            "source":                "TS_Engine.v1_5_9",
        }
        if tp_price is not None:
            rec["tp_price"] = float(tp_price)
        if bar_context:
            for k, v in bar_context.items():
                # only persist primitive types — defends parity diff against
                # numpy scalar comparison surprises
                if isinstance(v, (str, int, float, bool)) or v is None:
                    rec[k] = v
                else:
                    try:
                        rec[k] = float(v)
                    except (TypeError, ValueError):
                        rec[k] = repr(v)

        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            print(f"  SHADOW_JOURNAL_WRITE_ERROR  {strategy_id}  {bar_ts}  {e}")

    def write_marker(self, marker: str, detail: str = "") -> None:
        """Run-segmentation marker (RUN_START / RUN_END / etc.)."""
        rec = {
            "run_id":      self._run_id,
            "written_utc": _dt.datetime.utcnow().isoformat(timespec="seconds"),
            "event_type":  marker,
            "source":      "TS_Engine.v1_5_9",
        }
        if detail:
            rec["detail"] = detail
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            print(f"  SHADOW_JOURNAL_MARKER_ERROR  {marker}  {e}")


# ---------------------------------------------------------------------------
# OBSERVABILITY_CANONICAL_HASH_V1 — bar-level telemetry + exit journal
# Telemetry-only additions (no signal-path mutation, no engine touch).
# ---------------------------------------------------------------------------

class BarTelemetryJournal:
    """Append-only writer for the per-bar parity-tracking telemetry stream.

    One record per (strategy, bar) evaluated by the sidecar. Companion to
    ShadowJournal. Output file: bar_telemetry.jsonl in the same directory.

    Records carry exactly the fields specified in OBSERVABILITY_HARDENING_PLAN
    (input snapshot + decision snapshot + regime outputs) so the discriminator
    script can directly compare records keyed by (strategy_id, bar_ts) across
    runtimes.

    Never raises — logs on write error and continues.
    """

    def __init__(self, path: Path, run_id: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id

    def write(self, **fields: Any) -> None:
        rec: dict[str, Any] = {
            "run_id":      self._run_id,
            "written_utc": _dt.datetime.utcnow().isoformat(timespec="seconds"),
            "event_type":  "BAR_TELEMETRY",
            "source":      "TS_Engine.v1_5_9",
        }
        for k, v in fields.items():
            rec[k] = _coerce_json_safe(v)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            sid = fields.get("strategy_id", "?")
            bts = fields.get("bar_ts", "?")
            print(f"  BAR_TELEMETRY_WRITE_ERROR  {sid}  {bts}  {e}")


class ExitSignalJournal:
    """Append-only writer for sidecar exit-transition events.

    Exits are detected at the bar-loop layer by observing BarState.in_pos
    transitions (True -> False) across an evaluate_bar() call. The sidecar
    historically journaled only entries via ShadowJournal.write_signal();
    this writer closes the asymmetry vs TS_Execution's shadow_trades.jsonl.

    Output file: exit_signal_journal.jsonl in the same directory.

    Never raises.
    """

    def __init__(self, path: Path, run_id: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id

    def write(self, **fields: Any) -> None:
        rec: dict[str, Any] = {
            "run_id":      self._run_id,
            "written_utc": _dt.datetime.utcnow().isoformat(timespec="seconds"),
            "event_type":  "EXIT_SIGNAL",
            "source":      "TS_Engine.v1_5_9",
        }
        for k, v in fields.items():
            rec[k] = _coerce_json_safe(v)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            sid = fields.get("strategy_id", "?")
            bts = fields.get("bar_ts", "?")
            print(f"  EXIT_SIGNAL_WRITE_ERROR  {sid}  {bts}  {e}")


def _coerce_json_safe(v: Any) -> Any:
    """Convert numpy/pandas scalars to native Python primitives for JSON.

    Defensive: parity diff against `numpy.int64` / `numpy.bool_` would
    silently fail without this. Mirrors the pattern in ShadowJournal.write_signal.
    """
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    try:
        import numpy as _np
        if isinstance(v, _np.integer):
            return int(v)
        if isinstance(v, _np.bool_):
            return bool(v)
        if isinstance(v, _np.floating):
            f = float(v)
            return None if f != f else f  # NaN -> None
    except ImportError:
        pass
    try:
        import pandas as _pd
        if _pd.isna(v):
            return None
    except (ImportError, TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return repr(v)
