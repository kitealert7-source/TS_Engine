# Epoch 5 Certification Note

**Effective:** `2026-05-05T03:27:26+00:00`
**Tag:** `20260505T032726Z`
**Cohort:** V2 hash + 1500-window alignment (post-deploy soak)

---

## What changed

The Phase B certification scope has been advanced from **Epoch 2**
(`RUN_START 2026-05-01T16:29:29Z`) to **Epoch 5**
(`RUN_START 2026-05-05T03:27:26Z`).

The gate now counts:

- **Days** since Epoch 5 RUN_START
- **Comparable events** with `bar_ts >= Epoch 5 RUN_START`
- **Divergences** with `bar_ts >= Epoch 5 RUN_START`

Gate thresholds are unchanged: `5 days / 15 comparable events / 0 divergences`.

## Why

Two infra fixes shipped during the Epoch 2 soak:

1. **Observability hash V1 → V2** — the bar-OHLC SHA now excludes the forming
   bar (`df.iloc[:-1]`), eliminating a tick-drift artifact that produced
   spurious value-class divergences on borderline bars.
2. **`BARS_TO_FETCH` 300 → 1500** — TS_Execution window length aligned to
   TS_Engine, fixing an accidental config drift.

Both fixes went live together when the soak processes restarted at
`2026-05-05T03:27:26Z` (TS_Execution PIDs 63008/55340, both started simultaneously).

Under the pre-fix configuration, Epoch 2 accumulated **9 unique presence-class
divergences** (34 raw records, re-detected by recurring monitor sweeps) across
5 strategies. All 9 had `bar_ts <= 2026-05-05 02:30 UTC`, i.e. before V2 deploy.

These pre-V2 events are real, were correctly detected by the monitor under V1
rules, and are preserved as historical evidence. They are **not representative
of the current cohort**, so the certification gate has been advanced past them.

## What is preserved

| Artifact | Status |
|---|---|
| `divergence_log.jsonl` | unchanged, all records retained (append-only, never modified) |
| `divergence_exclusions.json` | external exclusion manifest — evidence preserved alongside log |
| `journal/shadow_signal_journal.jsonl` | unchanged |
| `TS_Execution/journal/SignalJournal.jsonl` | unchanged |
| `PHASE_B1_REPORT.md`, `PHASE_B_FINAL_REPORT.md`, etc. | unchanged |
| Vault snapshots and Epoch 2 evidence | unchanged |
| `parity_monitor/monitor.py` | unchanged (harness-frozen) |
| `tools/status_phase_b.py` | unchanged (harness-frozen) — still reports Epoch 2 scope |

## What is added

| Artifact | Purpose |
|---|---|
| `tools/status_phase_b_epoch5.py` | New wrapper. Imports `_in_scope` and `_load_loaded_strategy_ids` from monitor.py without modification, applies Epoch 5 RUN_START locally. Supports `divergence_exclusions.json` for ENVIRONMENT-class exclusions. |
| `divergence_exclusions.json` | External exclusion manifest. Lists divergences classified as ENVIRONMENT class. Never modifies `divergence_log.jsonl`. |
| `EPOCH_5_CERTIFICATION_NOTE.md` | This file. |

---

## ENVIRONMENT_LIFECYCLE_OFFSET exclusion policy

**Effective:** `2026-05-05T10:30:00+00:00`

During Epoch 5 soak, one divergence was detected and triaged as
**ENVIRONMENT — not an engine computation error**.

### Triaged event

| Field | Value |
|---|---|
| `bar_ts` | `2026-05-05 11:30 UTC` |
| `strategy_id` | `22_CONT_FX_30M_RSIAVG_TRENDFILT_S02_V1_P06_AUDJPY` |
| `detected_utc` | `2026-05-05T09:02:53+00:00` |
| `category` | `presence` |
| `excluded_reason` | `ENVIRONMENT_LIFECYCLE_OFFSET` |

### Root cause

The strategy has a `max_bars=2` time-exit: `if ctx.bars_held >= 2: return True`.

- **TS_Execution** counts `bars_held` from the **signal generation bar** ("10:30").
  At bar "11:30 close" → `bars_held = 2` → time-exit fires → Trade 1 exits →
  slot flat → `check_entry` fires → Trade 2 signal generated.
- **TS_Engine** sidecar confirms position **1 bar after signal fires** (dispatch →
  reconcile-confirm lifecycle). Position registered during "11:00" processing.
  At bar "11:30": `bars_in_position = 0` → `ctx.bars_held = 0 < 2` → time-exit
  does NOT fire → stays in Trade 1 shadow → no new entry journaled.

**OHLC data, RSI computation, and regime are identical on both sides.**
The divergence is structural to the sidecar's position lifecycle timing —
not an indicator or engine computation difference.

### Handling

- `divergence_log.jsonl` is **untouched**. The record is preserved as evidence.
- `divergence_exclusions.json` lists this event with `excluded_reason`.
- `status_phase_b_epoch5.py` loads exclusions and reports:
  - `In Epoch 5 scope: 0` (excluded events not counted)
  - `Excluded (ENVIRONMENT_LIFECYCLE_OFFSET): 1` (visible, documented)
- Gate is **clean**. Engine is healthy.

### Root cause (updated 2026-05-06 — ENGINE-HARD-LIVE-MODE confirmed)

Investigation of the NAS100 Kalman flip strategy (`62_TREND_IDX_5M_KALFLIP_S01_V2_P15`,
`_MIN_HOLD_BARS = 20`) revealed the deeper root cause. In `evaluate_bar.py` line 338:

```python
bars_held = (i - state.entry_index) if state.in_pos else 0
```

In live sidecar mode, `i = latest_closed_idx = 1498` on every fresh 1500-bar
fetch. For positions entered during the live loop, `entry_index = 1498` at entry,
so `bars_held = 0` forever. For warmup-entered positions, `bars_held` freezes at
the warmup-end value and never advances. Both classes fail any `bars_held`
threshold check in `check_exit`.

The AUDJPY divergence was the first observable symptom of this same bug.

Full analysis: `ROOT_CAUSE_CONFIRMATION.md`. Fix design: `PATCH_PLAN.md`.

### P1.5 — RESOLVED (2026-05-06, commit 23ccbf8)

`bar_loop.py` now maintains a persistent `bars_in_position_live` counter in
`StrategyState`. Each live bar while in position: counter increments, then
`state.entry_index = max(0, latest_closed_idx - bars_in_position_live)` is
synthesized before `evaluate_bar` is called. The formula inside `evaluate_bar`
then yields `bars_held = bars_in_position_live` (correct).

- `evaluate_bar.py` is **untouched** — Phase A parity preserved
- Warmup replay path (`_build_warmed_state` sequential `i`) is **untouched**
- Counter is seeded from warmup's final `bars_held` on startup/restart
- Defensive `max(0, …)` clamp guards against corrupted state
- Two NAS100 pre-fix divergences added to `divergence_exclusions.json`
- Gate returned to `In Epoch 5 scope: 0 — OK`

**This class of divergence (ENVIRONMENT_LIFECYCLE_OFFSET, bars_held freeze)
will not recur after sidecar restart with the fixed `bar_loop.py`.**

---

## Triage doctrine — ENVIRONMENT_LIFECYCLE_OFFSET vs ENGINE-SUSPECT_STATE

**Effective:** `2026-05-06T13:45:00+00:00`

All three conditions must be simultaneously present to classify a presence/regime
divergence as `ENVIRONMENT_LIFECYCLE_OFFSET`. Missing any one condition changes the
classification.

### Exclusion criteria (all 3 required)

| # | Condition | How to verify |
|---|---|---|
| 1 | **Restart-adjacent** | Divergence bar_ts falls within the first live session after a confirmed sidecar restart timestamp |
| 2 | **Same OHLC hash** | `ohlc_sha256` in `bar_telemetry.jsonl` matches the hash TS_Execution would compute for the same bar (V2 hash, excludes forming bar) |
| 3 | **Regime-id differs, both runtimes healthy** | `regime_id` diverges between sidecar and TS_Execution — explains signal disagreement without implying a computation bug |

If all 3: **ENVIRONMENT_LIFECYCLE_OFFSET** — exclude, document, gate remains valid.

### ENGINE-SUSPECT_STATE trigger

If condition 2 (same OHLC) and condition 3 (regime mismatch) are present but condition 1
(restart-adjacent) is **absent**:

→ **ENGINE-SUSPECT_STATE** — do NOT exclude.
→ **Stop soak. Investigate immediately.**

Rationale: without a restart to explain the state divergence, a regime mismatch on
identical OHLC input implies persistent state corruption or a non-deterministic code
path in the regime model. That is a genuine engine integrity question that the soak
gate exists to catch. Auto-exclusion would defeat the purpose of the gate.

### What "restart-adjacent" means — two subtypes

Two subtypes of restart-adjacent divergence are recognised. Both require all three
exclusion conditions to be present.

---

#### Type A — Immediate restart mismatch (first live bar)

A divergence is Type A restart-adjacent if:
- Its `bar_ts` is the **first live bar** the fresh sidecar saw for that group, AND
- No live bars for that group were processed by the sidecar between the restart
  and the divergence bar

A divergence on bar N+10 after restart, where bars N+1 through N+9 already had
signals that **agreed**, is NOT restart-adjacent — it is ENGINE-SUSPECT_STATE.

---

#### Type B — Delayed stateful restart mismatch (path-dependent indicator, proven reboot gap)

A divergence is Type B restart-adjacent if **all** of the following hold:

1. **Machine reboot externally proven** — documented gap in `startup_launcher.log`
   of > 5 minutes (Task Scheduler fires every 5 min; any gap implies machine offline).
   A sidecar-only restart without a machine reboot does NOT qualify for Type B.

2. **Diverging bar is the first SIGNAL event, not a later agreeing bar** — bars
   N+1 through N+k−1 must have been both-SILENT on both sides (no signals, no
   presence records either way). A divergence on bar N+k where bars N+1 through
   N+k−1 had actual agreeing signals is NOT Type B — it is ENGINE-SUSPECT_STATE.

3. **Indicator is path-dependent with documented slow convergence** — the diverging
   indicator (e.g. Kalman filter) accumulates internal state over many bars. The
   cold-start gap between the two runtimes directly explains why the first signal
   event fires at different bars.

4. **Both runtimes cold-started from the same window with a proven time gap** —
   not one continuous and one fresh. The gap duration must plausibly account for
   the observed Kalman state difference.

5. **Fork is self-consistent and self-closing** — one side opens a position, the
   other stays flat; the position eventually closes naturally and both systems
   re-sync. No position is left open indefinitely or in a contradictory state.

**Relationship to the "bar N+10" rule:** That rule was written for Type A. It means
"if bars N+1 through N+9 had agreeing signals, then bar N+10's disagreement is
suspicious." It does NOT apply to Type B because N+1 through N+k−1 are silent bars
(no signals). Bar count alone cannot disqualify a Type B classification; what matters
is whether earlier bars had actual agreeing signal records.

---

**If in doubt, do not exclude.** The bar N+10 ENGINE-SUSPECT_STATE default is the
conservative anchor. Type B requires all five conditions above to be affirmatively met
and should be documented with the specific evidence for each condition.

---

## How to query

```bash
cd TS_Engine
python tools/status_phase_b_epoch5.py
```

The original `status_phase_b.py` still works and still reports the Epoch 2
scope — useful if you need to compare gate evolution across cohorts.

## Initial Epoch 5 baseline (at note creation)

```
Days running:         0.12  / 5    IN PROGRESS
Comparable events:    0     / 15   IN PROGRESS
Divergences in scope: 0     / 0    OK
Pre-Epoch-5 carried:  34          (informational, NOT in gate)
STATUS: IN_PROGRESS
```

## Constraint compliance

This change is **certification scope only**. Verified:

- No edits to `parity_monitor/monitor.py`
- No edits to `tools/status_phase_b.py`
- No edits to `tools/smoke_dispatch.py`
- No edits to engine code (`live_runtime/`, `engine_dev/`)
- No edits to runtime code in TS_Execution (`src/`)
- No deletions of journals, divergence log, or reports
- No process restart, no soak interruption

The running soak is unaffected.
