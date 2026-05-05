"""
status_phase_b.py — print Phase B gate progress.

Reports:
  - Days running (since first RUN_START marker in shadow journal)
  - TS_Execution signal count (in scope)
  - TS_Engine shadow signal count (in scope)
  - Comparable events (parity-confirmed bars within scope)
  - Divergence count
  - Gate status: PASS / FAIL / IN_PROGRESS

Gate semantics (updated per user decision 2026-05-02):
  A "comparable event" is any bar evaluation where both runtimes agreed:
    - Both emitted a signal   → counted here (observable in journals)
    - Both produced no-signal → parity confirmed, but NOT currently counted
                                (neither journal records silent evaluations;
                                 this count is therefore a lower bound on
                                 true comparable events)
    - Mismatch                → divergence, not counted as parity evidence

  Gate: >= 5 calendar days AND >= 15 comparable events AND 0 divergences.

Scope: only bars within the Epoch 2 window (bar_ts >= PHASE_B1_RUN_START_UTC)
       and strategy_id in the current burn-in portfolio. Mirrors monitor.py.

Read-only. Touches no journals.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

TS_ENGINE_ROOT = Path(__file__).resolve().parents[1]
TS_EXECUTION_ROOT = TS_ENGINE_ROOT.parent / "TS_Execution"

if str(TS_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(TS_ENGINE_ROOT))

from parity_monitor.compare import parse_bar_ts          # noqa: E402
from parity_monitor.monitor import (                     # noqa: E402
    PHASE_B1_RUN_START_UTC,
    PHASE_B1_RUN_START_TAG,
    _load_loaded_strategy_ids,
    _in_scope,
)

EXEC_JOURNAL   = TS_EXECUTION_ROOT / "journal" / "SignalJournal.jsonl"
SHADOW_JOURNAL = TS_ENGINE_ROOT / "journal" / "shadow_signal_journal.jsonl"
DIVERGENCE_LOG = TS_ENGINE_ROOT / "divergence_log.jsonl"

GATE_DAYS        = 5
GATE_EVENTS      = 15   # comparable events (lower bound — both-silent not counted)
GATE_DIVERGENCES = 0


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                out.append(json.loads(s))
            except json.JSONDecodeError:
                continue
    return out


def _signal_only(recs: list[dict]) -> list[dict]:
    return [r for r in recs if "event_type" not in r and "signal" in r]


def _first_run_start(recs: list[dict]) -> datetime | None:
    for r in recs:
        if r.get("event_type") == "RUN_START":
            ts = r.get("written_utc")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
    return None


def _key(r: dict):
    sid = r.get("strategy_id")
    bts = r.get("bar_ts")
    if not sid or not bts:
        return None
    try:
        return (sid, parse_bar_ts(bts))
    except (ValueError, TypeError):
        return None


def main() -> int:
    print("=" * 78)
    print(f"Phase B status — {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  Scope: bar_ts >= {PHASE_B1_RUN_START_UTC.isoformat(timespec='seconds')} "
          f"(tag={PHASE_B1_RUN_START_TAG})")
    print("=" * 78)

    allowed_ids = _load_loaded_strategy_ids()

    exec_recs_all  = _read_jsonl(EXEC_JOURNAL)
    shadow_recs_all = _read_jsonl(SHADOW_JOURNAL)
    div_recs        = _read_jsonl(DIVERGENCE_LOG)

    # Apply scope filter (mirrors monitor.py)
    exec_signals_all   = _signal_only(exec_recs_all)
    shadow_signals_all = _signal_only(shadow_recs_all)

    exec_signals   = [r for r in exec_signals_all
                      if _in_scope(r, PHASE_B1_RUN_START_UTC, allowed_ids)[0]]
    shadow_signals = [r for r in shadow_signals_all
                      if _in_scope(r, PHASE_B1_RUN_START_UTC, allowed_ids)[0]]

    run_start = _first_run_start(shadow_recs_all)
    if run_start:
        elapsed = datetime.now(timezone.utc) - run_start
        days = elapsed.total_seconds() / 86400.0
    else:
        elapsed = None
        days = 0.0

    # Comparable events: bars where both sides agree (both emitted a signal)
    # NOTE: bars where both sides correctly produced no-signal are also valid
    # comparable events, but are not recorded in either journal and cannot be
    # counted here. This number is a lower bound.
    exec_keys   = {_key(r) for r in exec_signals   if _key(r)}
    shadow_keys = {_key(r) for r in shadow_signals if _key(r)}
    comparable_events = exec_keys & shadow_keys   # both present and agreeable
    only_exec   = exec_keys   - shadow_keys
    only_shadow = shadow_keys - exec_keys

    n_comparable = len(comparable_events)

    # Gate evaluation
    days_ok   = days >= GATE_DAYS
    events_ok = n_comparable >= GATE_EVENTS
    div_ok    = len(div_recs) <= GATE_DIVERGENCES

    overall_pass = days_ok and events_ok and div_ok
    overall_fail = (not div_ok) or (run_start is None)

    print(f"\nRuntime")
    if run_start:
        print(f"  RUN_START:            {run_start.isoformat(timespec='seconds')}")
        print(f"  Elapsed:              {elapsed}")
        print(f"  Days running:         {days:.2f}  (gate: >= {GATE_DAYS})  "
              f"{'OK' if days_ok else 'IN PROGRESS'}")
    else:
        print(f"  RUN_START:            not found in shadow journal (sidecar not started?)")

    print(f"\nComparable events")
    print(f"  Exec in scope:        {len(exec_signals)} signal records")
    print(f"  Shadow in scope:      {len(shadow_signals)} signal records")
    print(f"  Comparable events:    {n_comparable} / {GATE_EVENTS}  "
          f"{'OK' if events_ok else 'IN PROGRESS'}")
    print(f"  (lower bound — both-silent bars not journaled)")
    print(f"  Exec only (pending):  {len(only_exec)}")
    print(f"  Shadow only (pending):{len(only_shadow)}")

    print(f"\nDivergences")
    print(f"  Recorded:             {len(div_recs)}  (gate: <= {GATE_DIVERGENCES})  "
          f"{'OK' if div_ok else 'FAIL'}")
    if div_recs:
        by_field: dict[str, int] = {}
        for d in div_recs:
            fld = d.get("field", "?")
            by_field[fld] = by_field.get(fld, 0) + 1
        print(f"  By field:")
        for fld, n in sorted(by_field.items(), key=lambda x: -x[1])[:8]:
            print(f"    {fld:24s}  {n}")

    print(f"\nGate")
    if overall_fail:
        print(f"  STATUS: FAIL  (divergences recorded: {len(div_recs)})")
        rc = 2
    elif overall_pass:
        print(f"  STATUS: PASS  — Phase B gate met. Authorize Phase C.")
        rc = 0
    else:
        print(f"  STATUS: IN_PROGRESS")
        if not days_ok:
            print(f"    waiting on: {GATE_DAYS - days:.1f} more days")
        if not events_ok:
            print(f"    waiting on: {GATE_EVENTS - n_comparable} more comparable events")
        rc = 1

    print("=" * 78)
    return rc


if __name__ == "__main__":
    sys.exit(main())
