"""
compare.py — field-by-field parity comparison between TS_Execution and TS_Engine.

Inputs are two signal records (dicts) from each side's journal. Output is
a list of divergence dicts, one per field that differs.

Comparison semantics (per Phase B GO prompt):
  - signal present / absent
  - direction
  - stop
  - target
  - regime
  - timestamps

Normalization rules (applied before comparison to suppress trivial
formatting differences that aren't real divergences):

  - bar_ts: parsed to datetime in UTC; equality is on the datetime, not
    the string format. "2026-05-01 03:15 UTC" == "2026-05-01T03:15:00Z".
  - floats: compared with eps=1e-6 (handles JSON float round-trip noise)
  - regime fields: numpy ints round-tripped to JSON come back as Python
    ints; string regime labels compared raw.

Anything outside these normalizations is a real divergence.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone


FLOAT_EPS = 1e-6

# Fields the parity gate explicitly cares about (per GO prompt)
COMPARE_FIELDS = [
    "signal",                  # direction
    "entry_reference_price",
    "stop_price",
    "tp_price",                # target
    "entry_reason",
    # Regime fields (subset that matches both sides)
    "volatility_regime",
    "trend_regime",
    "trend_label",
    "market_regime",
    # Bar OHLC at signal time
    "bar_high",
    "bar_low",
    "bar_close",
]


def parse_bar_ts(s) -> datetime:
    """Parse bar_ts in any of TS_Execution / TS_Engine formats to UTC datetime.
    Raises ValueError if unparseable."""
    if isinstance(s, datetime):
        return s.astimezone(timezone.utc) if s.tzinfo else s.replace(tzinfo=timezone.utc)
    if not isinstance(s, str):
        raise ValueError(f"bar_ts is not str/datetime: {type(s).__name__}")

    # Try ISO formats first
    s_clean = s.strip()
    iso_attempts = [
        s_clean,
        s_clean.replace("Z", "+00:00"),
        re.sub(r"\s+UTC$", "+00:00", s_clean),
        re.sub(r"\s+UTC$", "", s_clean),
    ]
    for attempt in iso_attempts:
        try:
            dt = datetime.fromisoformat(attempt)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

    # TS_Execution format: "2026-05-01 03:15 UTC"
    try:
        dt = datetime.strptime(s_clean, "%Y-%m-%d %H:%M UTC")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    # TS_Execution format with seconds
    try:
        dt = datetime.strptime(s_clean, "%Y-%m-%d %H:%M:%S UTC")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    # TS_Execution alternative format: "2026-05-01 04:00 SVR"
    # Both sides emit bar_ts from the same MT5 epoch via utcfromtimestamp.
    # The "SVR" suffix in TS_Execution's pipeline.py::_bar_ts_str is a label
    # only — the numeric value is identical to the "UTC"-suffixed value.
    # Verified by source inspection of:
    #   TS_Execution/src/pipeline.py::_bar_ts_str (line ~427)
    #   TS_Engine/live_runtime/bar_loop.py::_format_bar_ts_for_journal
    # Approved by user authorization 2026-05-01.
    try:
        dt = datetime.strptime(s_clean, "%Y-%m-%d %H:%M SVR")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    try:
        dt = datetime.strptime(s_clean, "%Y-%m-%d %H:%M:%S SVR")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    raise ValueError(f"unparseable bar_ts: {s!r}")


def _is_present(record: dict | None) -> bool:
    """A signal is 'present' if it's a non-None record with a 'signal' field."""
    return record is not None and "signal" in record


def _equal_floats(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        af = float(a)
        bf = float(b)
    except (TypeError, ValueError):
        return False
    if math.isnan(af) and math.isnan(bf):
        return True
    if math.isnan(af) or math.isnan(bf):
        return False
    if af == bf:
        return True
    return abs(af - bf) <= FLOAT_EPS * max(1.0, abs(af), abs(bf))


def _equal_field(field: str, a, b) -> bool:
    """Field-aware equality."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False

    if field in ("entry_reference_price", "stop_price", "tp_price",
                 "bar_high", "bar_low", "bar_close"):
        return _equal_floats(a, b)

    if field == "signal":
        try:
            return int(a) == int(b)
        except (TypeError, ValueError):
            return a == b

    # Categorical/integer regime fields
    if field in ("volatility_regime", "trend_regime"):
        # Could be int or str depending on source; normalize
        try:
            return int(a) == int(b)
        except (TypeError, ValueError):
            return str(a).strip() == str(b).strip()

    # String fields (entry_reason, trend_label, market_regime)
    return str(a).strip() == str(b).strip()


def compare_records(ts_exec_rec: dict | None, ts_engine_rec: dict | None,
                    *, strategy_id: str, bar_ts: str) -> list[dict]:
    """Compare two records; return list of divergence dicts (one per field).

    A divergence dict has shape:
      {
        "strategy_id": ...,
        "bar_ts":      ...,
        "field":       <field name | "presence">,
        "ts_execution": <value>,
        "ts_engine":    <value>,
        "category":     "presence" | "value",
      }
    """
    diffs: list[dict] = []
    exec_present = _is_present(ts_exec_rec)
    engine_present = _is_present(ts_engine_rec)

    # Presence check first
    if exec_present != engine_present:
        diffs.append({
            "strategy_id":  strategy_id,
            "bar_ts":       bar_ts,
            "field":        "presence",
            "ts_execution": "PRESENT" if exec_present else "ABSENT",
            "ts_engine":    "PRESENT" if engine_present else "ABSENT",
            "category":     "presence",
        })
        return diffs   # don't compare fields if one side is missing

    if not (exec_present and engine_present):
        return diffs   # both absent — that's parity (NO_SIGNAL agrees)

    # Field-by-field
    for field in COMPARE_FIELDS:
        a = ts_exec_rec.get(field)
        b = ts_engine_rec.get(field)
        if a is None and b is None:
            continue   # both missing this optional field → OK
        if not _equal_field(field, a, b):
            diffs.append({
                "strategy_id":  strategy_id,
                "bar_ts":       bar_ts,
                "field":        field,
                "ts_execution": a,
                "ts_engine":    b,
                "category":     "value",
            })
    return diffs
