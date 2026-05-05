"""
monitor.py — Phase B parity daemon.

Tails:
  - TS_Execution/journal/SignalJournal.jsonl   (authoritative side)
  - TS_Engine/journal/shadow_signal_journal.jsonl  (observer side)

Aligns by (strategy_id, bar_ts). For each pair, calls
parity_monitor.compare.compare_records(). Any divergence is appended to
TS_Engine/divergence_log.jsonl.

Reads only — never modifies either source journal. Writes only to its own
divergence_log.jsonl.

Heartbeat: writes a status line to stdout every 60s with cumulative counts.

Presence-divergence grace period (PRESENCE_GRACE_S = 90):
  TS_Execution and TS_Engine sidecar both write to their journals shortly
  after bar close. With a 30s poll the monitor can catch one side before the
  other has flushed, producing a spurious presence divergence. The grace
  period requires a presence mismatch to persist for >= 90s (3 poll cycles)
  before it is written to divergence_log. Value divergences (both records
  present, fields differ) are still logged immediately.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

TS_ENGINE_ROOT = Path(__file__).resolve().parents[1]
TS_EXECUTION_ROOT = TS_ENGINE_ROOT.parent / "TS_Execution"

# Allow .compare import when run as a script
if str(TS_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(TS_ENGINE_ROOT))

from parity_monitor.compare import compare_records, parse_bar_ts  # noqa: E402

EXEC_JOURNAL  = TS_EXECUTION_ROOT / "journal" / "SignalJournal.jsonl"
SHADOW_JOURNAL = TS_ENGINE_ROOT / "journal" / "shadow_signal_journal.jsonl"
DIVERGENCE_LOG = TS_ENGINE_ROOT / "divergence_log.jsonl"
PORTFOLIO_YAML = TS_EXECUTION_ROOT / "portfolio.yaml"

# ---------------------------------------------------------------------------
# Phase B1 scope filters (added per Phase B1 STOP REPORT findings)
#
# The monitor compares only records that BOTH:
#   1. have bar_ts >= PHASE_B1_RUN_START_UTC, AND
#   2. have strategy_id in the current burn-in portfolio.
#
# Records outside scope (historical bars, removed strategies) are silently
# excluded from comparison and counted as historical_records_ignored.
# ---------------------------------------------------------------------------

# Phase B1 Epoch 2 run start: matches TS_Engine sidecar start time
# (run_id PHASE_B2_20260501T162200Z, RUN_START written_utc 2026-05-01T16:29:29Z).
# Set to sidecar start so only bars the sidecar could have observed are in scope.
# Format: YYYYMMDDTHHmmssZ
PHASE_B1_RUN_START_TAG = "20260501T162929Z"
PHASE_B1_RUN_START_UTC = datetime.strptime(
    PHASE_B1_RUN_START_TAG, "%Y%m%dT%H%M%SZ"
).replace(tzinfo=timezone.utc)


def _load_loaded_strategy_ids() -> set[str]:
    """Read TS_Execution/portfolio.yaml; return set of enabled strategy IDs.
    These are the strategies TS_Engine sidecar will load and evaluate."""
    with open(PORTFOLIO_YAML, encoding="utf-8") as f:
        full = yaml.safe_load(f)
    strats = (full.get("portfolio") or {}).get("strategies") or []
    return {s["id"] for s in strats
            if s.get("id") and s.get("enabled", True)}


def _in_scope(rec: dict, run_start_dt: datetime, allowed_ids: set[str]
              ) -> tuple[bool, str | None]:
    """Returns (in_scope, reason_if_not).

    Reasons (used for diagnostic counting only — never logged as divergence):
      'unknown_strategy' : strategy_id not in current portfolio
      'pre_run_start'    : bar_ts before Phase B1 RUN_START
      'malformed'        : missing or unparseable strategy_id / bar_ts
    """
    sid = rec.get("strategy_id")
    bts = rec.get("bar_ts")
    if not sid or not bts:
        return False, "malformed"
    if sid not in allowed_ids:
        return False, "unknown_strategy"
    try:
        bts_dt = parse_bar_ts(bts)
    except (ValueError, TypeError):
        return False, "malformed"
    if bts_dt < run_start_dt:
        return False, "pre_run_start"
    return True, None


def _safe_parse_bar_ts(s):
    try:
        return parse_bar_ts(s)
    except (ValueError, TypeError):
        return None


def _key_for(rec: dict) -> tuple[str, datetime] | None:
    """Alignment key: (strategy_id, parsed bar_ts)."""
    sid = rec.get("strategy_id")
    bts = rec.get("bar_ts")
    if not sid or not bts:
        return None
    parsed = _safe_parse_bar_ts(bts)
    if parsed is None:
        return None
    return (sid, parsed)


def _is_signal_record(rec: dict) -> bool:
    """Skip RUN_START/RUN_END markers and other non-signal events."""
    if "event_type" in rec:
        return False
    if "signal" not in rec:
        return False
    return True


def _read_jsonl(path: Path) -> list[dict]:
    """Read all signal records from a JSONL file (skips markers/blank lines)."""
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _is_signal_record(rec):
                out.append(rec)
    return out


def _filter_in_scope(recs: list[dict], run_start_dt: datetime,
                     allowed_ids: set[str]) -> tuple[list[dict], dict[str, int]]:
    """Apply Phase B1 scope bounds. Returns (in_scope_records, exclusion_counts)."""
    in_scope = []
    excluded: dict[str, int] = {"unknown_strategy": 0,
                                "pre_run_start": 0,
                                "malformed": 0}
    for r in recs:
        ok, reason = _in_scope(r, run_start_dt, allowed_ids)
        if ok:
            in_scope.append(r)
        elif reason in excluded:
            excluded[reason] += 1
    return in_scope, excluded


def _write_divergence(div: dict) -> None:
    DIVERGENCE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(DIVERGENCE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(div, sort_keys=True, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


# Presence divergences must persist for this long before being logged.
# Eliminates race-condition false positives where one side writes within
# one poll cycle of the other (observed race window: 6–28 seconds).
PRESENCE_GRACE_S: int = 90   # 3 × default poll interval


def _bar_ts_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def run_monitor_loop(poll_interval_s: int = 30, heartbeat_interval_s: int = 60) -> int:
    print("=" * 78)
    print(f"TS_Engine parity monitor")
    print(f"  exec journal:    {EXEC_JOURNAL}")
    print(f"  shadow journal:  {SHADOW_JOURNAL}")
    print(f"  divergence log:  {DIVERGENCE_LOG}")
    print(f"  poll interval:   {poll_interval_s}s")
    print("=" * 78)

    # ----- Phase B1 scope: load filters and print self-check banner -----
    allowed_ids = _load_loaded_strategy_ids()

    # Initial scope inventory: count records that exist now but will be ignored
    initial_exec_recs = _read_jsonl(EXEC_JOURNAL)
    _, initial_excl = _filter_in_scope(initial_exec_recs,
                                       PHASE_B1_RUN_START_UTC, allowed_ids)
    historical_ignored = (initial_excl["unknown_strategy"]
                          + initial_excl["pre_run_start"]
                          + initial_excl["malformed"])

    print(f"\nMONITOR_SCOPE:")
    print(f"  start_ts={PHASE_B1_RUN_START_UTC.isoformat()} "
          f"(tag={PHASE_B1_RUN_START_TAG})")
    print(f"  strategies={len(allowed_ids)}")
    for sid in sorted(allowed_ids):
        print(f"    - {sid}")
    print(f"  historical_records_ignored={historical_ignored}")
    print(f"    pre_run_start:    {initial_excl['pre_run_start']}")
    print(f"    unknown_strategy: {initial_excl['unknown_strategy']}")
    print(f"    malformed:        {initial_excl['malformed']}")
    print(f"  exec_total_records={len(initial_exec_recs)}")
    print(f"  exec_in_scope_at_start="
          f"{len(initial_exec_recs) - historical_ignored}")
    print("=" * 78)
    print()

    # Persisted across iterations to avoid re-reporting same divergences
    seen_diff_keys: set[tuple] = set()
    # Grace-period staging for presence divergences.
    # Maps diff_key -> (first_seen_epoch, divergence_dict)
    pending_presence: dict[tuple, tuple[float, dict]] = {}
    last_heartbeat = 0.0
    cum_pairs = 0
    cum_divergences = 0

    try:
        while True:
            now_t = time.time()

            # Reload both journals (small files; cheap to re-read)
            exec_recs_all = _read_jsonl(EXEC_JOURNAL)
            shadow_recs_all = _read_jsonl(SHADOW_JOURNAL)

            # Apply Phase B1 scope filters
            exec_recs, exec_excl = _filter_in_scope(
                exec_recs_all, PHASE_B1_RUN_START_UTC, allowed_ids)
            shadow_recs, shadow_excl = _filter_in_scope(
                shadow_recs_all, PHASE_B1_RUN_START_UTC, allowed_ids)

            # Index by (strategy_id, bar_ts_dt)
            exec_idx: dict[tuple, dict] = {}
            for r in exec_recs:
                k = _key_for(r)
                if k:
                    exec_idx[k] = r
            shadow_idx: dict[tuple, dict] = {}
            for r in shadow_recs:
                k = _key_for(r)
                if k:
                    shadow_idx[k] = r

            # Find pairs to compare. Compare the union: any (sid, bar_ts)
            # in either journal. If only on one side → presence divergence.
            all_keys = set(exec_idx.keys()) | set(shadow_idx.keys())
            new_divs = 0

            # Track which presence diff_keys are still divergent this cycle
            active_presence_keys: set[tuple] = set()

            for k in sorted(all_keys, key=lambda x: (x[1], x[0])):
                sid, bts_dt = k
                bts_str = _bar_ts_str(bts_dt)
                exec_rec = exec_idx.get(k)
                shadow_rec = shadow_idx.get(k)
                diffs = compare_records(
                    exec_rec, shadow_rec,
                    strategy_id=sid,
                    bar_ts=bts_str,
                )
                cum_pairs += 1
                for d in diffs:
                    diff_key = (d["strategy_id"], d["bar_ts"], d["field"])
                    if diff_key in seen_diff_keys:
                        continue

                    if d["category"] == "presence":
                        # Grace-period: don't log immediately. Stage it and
                        # only confirm after PRESENCE_GRACE_S seconds.
                        active_presence_keys.add(diff_key)
                        if diff_key not in pending_presence:
                            pending_presence[diff_key] = (now_t, d)
                            elapsed = 0.0
                            print(f"  PRESENCE_PENDING  {d['strategy_id']}  "
                                  f"{d['bar_ts']}  "
                                  f"exec={d['ts_execution']!r}  "
                                  f"engine={d['ts_engine']!r}  "
                                  f"(grace={PRESENCE_GRACE_S}s)")
                        else:
                            first_t, orig_d = pending_presence[diff_key]
                            elapsed = now_t - first_t
                            if elapsed >= PRESENCE_GRACE_S:
                                # Still absent after grace period — real
                                seen_diff_keys.add(diff_key)
                                pending_presence.pop(diff_key)
                                orig_d["detected_utc"] = datetime.now(
                                    timezone.utc).isoformat(timespec="seconds")
                                orig_d["grace_elapsed_s"] = round(elapsed, 1)
                                _write_divergence(orig_d)
                                new_divs += 1
                                cum_divergences += 1
                                print(f"  DIVERGENCE  {orig_d['strategy_id']}  "
                                      f"{orig_d['bar_ts']}  "
                                      f"field={orig_d['field']}  "
                                      f"exec={orig_d['ts_execution']!r}  "
                                      f"engine={orig_d['ts_engine']!r}  "
                                      f"(confirmed after {elapsed:.0f}s)")
                    else:
                        # Value divergence — both records present, no race.
                        # Log immediately.
                        seen_diff_keys.add(diff_key)
                        d["detected_utc"] = datetime.now(
                            timezone.utc).isoformat(timespec="seconds")
                        _write_divergence(d)
                        new_divs += 1
                        cum_divergences += 1
                        print(f"  DIVERGENCE  {d['strategy_id']}  {d['bar_ts']}  "
                              f"field={d['field']}  exec={d['ts_execution']!r}  "
                              f"engine={d['ts_engine']!r}")

            # Resolve pending presence entries that are no longer divergent
            # (both sides have now written — race resolved within grace period)
            for diff_key in list(pending_presence.keys()):
                if diff_key not in active_presence_keys:
                    first_t, orig_d = pending_presence.pop(diff_key)
                    elapsed = now_t - first_t
                    print(f"  PRESENCE_RESOLVED  {orig_d['strategy_id']}  "
                          f"{orig_d['bar_ts']}  "
                          f"exec={orig_d['ts_execution']!r}  "
                          f"engine={orig_d['ts_engine']!r}  "
                          f"resolved_after={elapsed:.0f}s")

            # Heartbeat
            if now_t - last_heartbeat >= heartbeat_interval_s:
                last_heartbeat = now_t
                print(f"  HB  exec_in_scope={len(exec_recs)}  "
                      f"shadow_in_scope={len(shadow_recs)}  "
                      f"unique_pairs={len(all_keys)}  cum_div={cum_divergences}  "
                      f"new_this_loop={new_divs}  "
                      f"pending_presence={len(pending_presence)}  "
                      f"exec_ignored={len(exec_recs_all) - len(exec_recs)}")

            time.sleep(poll_interval_s)
    except KeyboardInterrupt:
        print("\n  monitor stopped (Ctrl-C)")
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description="TS_Engine Phase B parity monitor")
    p.add_argument("--poll", type=int, default=30,
                   help="Poll interval (seconds)")
    p.add_argument("--heartbeat", type=int, default=60,
                   help="Heartbeat interval (seconds)")
    args = p.parse_args()
    return run_monitor_loop(poll_interval_s=args.poll,
                            heartbeat_interval_s=args.heartbeat)


if __name__ == "__main__":
    sys.exit(main())
