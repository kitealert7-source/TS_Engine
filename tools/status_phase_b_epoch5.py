"""
Phase B — Epoch 5 certification status.

Epoch 5 = the V2-hash + 1500-window-aligned soak cohort.
RUN_START = 2026-05-05T03:27:26+00:00 (TS_Execution sidecar process StartTime,
                                       immediately after V2 hash deploy at ~03:26Z).

Why Epoch 5:
  - During Epoch 2 (RUN_START 2026-05-01T16:29:29Z), 9 unique presence-class
    divergences accumulated under V1 hash + window mismatch (300 vs 1500).
  - V2 hash + 1500-window alignment shipped 2026-05-05 ~03:27Z.
  - Epoch 5 measures gate progress on the post-V2 cohort only.

What is preserved (NOT deleted):
  - divergence_log.jsonl              (raw, untouched)
  - PHASE_B1_REPORT.md, PHASE_B_FINAL_REPORT.md, etc.
  - vault snapshots and archives

What changes:
  - Certification scope only. Gate counts bars with bar_ts >= Epoch 5 RUN_START.
  - No monitor.py / status_phase_b.py / engine / runtime edits.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TS_ENGINE_ROOT = Path(__file__).resolve().parents[1]
TS_EXECUTION_ROOT = TS_ENGINE_ROOT.parent / "TS_Execution"

if str(TS_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(TS_ENGINE_ROOT))

from parity_monitor.compare import parse_bar_ts          # noqa: E402
from parity_monitor.monitor import (                     # noqa: E402
    _load_loaded_strategy_ids,
    _in_scope,
)

# ---------------------------------------------------------------------------
# Epoch 5 scope (LOCAL OVERRIDE — does not modify monitor.py)
# ---------------------------------------------------------------------------
EPOCH_5_RUN_START_TAG = "20260505T032726Z"
EPOCH_5_RUN_START_UTC = datetime.strptime(
    EPOCH_5_RUN_START_TAG, "%Y%m%dT%H%M%SZ"
).replace(tzinfo=timezone.utc)

EXEC_JOURNAL   = TS_EXECUTION_ROOT / "journal" / "SignalJournal.jsonl"
SHADOW_JOURNAL = TS_ENGINE_ROOT / "journal" / "shadow_signal_journal.jsonl"
DIVERGENCE_LOG = TS_ENGINE_ROOT / "divergence_log.jsonl"

GATE_DAYS        = 5
GATE_EVENTS      = 15
GATE_DIVERGENCES = 0


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
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


def _key(r: dict):
    sid = r.get("strategy_id")
    bts = r.get("bar_ts")
    if not sid or not bts:
        return None
    try:
        return (sid, parse_bar_ts(bts))
    except (ValueError, TypeError):
        return None


def _div_bar_ts(d: dict) -> datetime | None:
    """Parse divergence record bar_ts ('YYYY-MM-DD HH:MM UTC' or ISO)."""
    bts = d.get("bar_ts")
    if not bts:
        return None
    try:
        return parse_bar_ts(bts)
    except (ValueError, TypeError):
        return None


def main() -> int:
    print("=" * 78)
    print(f"Phase B — Epoch 5 status — "
          f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  Scope: bar_ts >= {EPOCH_5_RUN_START_UTC.isoformat(timespec='seconds')} "
          f"(tag={EPOCH_5_RUN_START_TAG})")
    print(f"  Cohort: V2 hash + 1500-window alignment (post-deploy)")
    print("=" * 78)

    allowed_ids = _load_loaded_strategy_ids()

    exec_recs_all   = _read_jsonl(EXEC_JOURNAL)
    shadow_recs_all = _read_jsonl(SHADOW_JOURNAL)
    div_recs_all    = _read_jsonl(DIVERGENCE_LOG)

    exec_signals_all   = _signal_only(exec_recs_all)
    shadow_signals_all = _signal_only(shadow_recs_all)

    exec_signals = [
        r for r in exec_signals_all
        if _in_scope(r, EPOCH_5_RUN_START_UTC, allowed_ids)[0]
    ]
    shadow_signals = [
        r for r in shadow_signals_all
        if _in_scope(r, EPOCH_5_RUN_START_UTC, allowed_ids)[0]
    ]

    # Divergences: filter by bar_ts >= Epoch 5 RUN_START.
    div_recs_in_scope: list[dict] = []
    div_recs_pre_epoch: list[dict] = []
    for d in div_recs_all:
        bts = _div_bar_ts(d)
        if bts is None:
            continue
        if bts >= EPOCH_5_RUN_START_UTC:
            div_recs_in_scope.append(d)
        else:
            div_recs_pre_epoch.append(d)

    elapsed = datetime.now(timezone.utc) - EPOCH_5_RUN_START_UTC
    days = elapsed.total_seconds() / 86400.0

    exec_keys   = {_key(r) for r in exec_signals   if _key(r)}
    shadow_keys = {_key(r) for r in shadow_signals if _key(r)}
    comparable  = exec_keys & shadow_keys
    only_exec   = exec_keys   - shadow_keys
    only_shadow = shadow_keys - exec_keys

    n_comparable = len(comparable)

    days_ok   = days >= GATE_DAYS
    events_ok = n_comparable >= GATE_EVENTS
    div_ok    = len(div_recs_in_scope) <= GATE_DIVERGENCES

    overall_pass = days_ok and events_ok and div_ok
    overall_fail = not div_ok

    print(f"\nRuntime")
    print(f"  Epoch 5 RUN_START:    {EPOCH_5_RUN_START_UTC.isoformat(timespec='seconds')}")
    print(f"  Elapsed:              {elapsed}")
    print(f"  Days running:         {days:.2f}  (gate: >= {GATE_DAYS})  "
          f"{'OK' if days_ok else 'IN PROGRESS'}")

    print(f"\nComparable events (Epoch 5 scope)")
    print(f"  Exec in scope:        {len(exec_signals)} signal records")
    print(f"  Shadow in scope:      {len(shadow_signals)} signal records")
    print(f"  Comparable events:    {n_comparable} / {GATE_EVENTS}  "
          f"{'OK' if events_ok else 'IN PROGRESS'}")
    print(f"  (lower bound — both-silent bars not journaled)")
    print(f"  Exec only (pending):  {len(only_exec)}")
    print(f"  Shadow only (pending):{len(only_shadow)}")

    print(f"\nDivergences")
    print(f"  In Epoch 5 scope:     {len(div_recs_in_scope)}  "
          f"(gate: <= {GATE_DIVERGENCES})  "
          f"{'OK' if div_ok else 'FAIL'}")
    print(f"  Pre-Epoch-5 (carried):{len(div_recs_pre_epoch)}  "
          f"(preserved in divergence_log.jsonl, NOT counted in gate)")
    if div_recs_in_scope:
        by_field: dict[str, int] = {}
        for d in div_recs_in_scope:
            fld = d.get("field", "?")
            by_field[fld] = by_field.get(fld, 0) + 1
        print(f"  By field (in-scope only):")
        for fld, n in sorted(by_field.items(), key=lambda x: -x[1])[:8]:
            print(f"    {fld:24s}  {n}")

    print(f"\nGate")
    if overall_fail:
        print(f"  STATUS: FAIL  (in-scope divergences: {len(div_recs_in_scope)})")
        rc = 2
    elif overall_pass:
        print(f"  STATUS: PASS  — Epoch 5 gate met. Authorize Phase C.")
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
