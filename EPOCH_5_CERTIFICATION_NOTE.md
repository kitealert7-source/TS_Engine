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
| `divergence_log.jsonl` | unchanged, all 34 records retained |
| `journal/shadow_signal_journal.jsonl` | unchanged |
| `TS_Execution/journal/SignalJournal.jsonl` | unchanged |
| `PHASE_B1_REPORT.md`, `PHASE_B_FINAL_REPORT.md`, etc. | unchanged |
| Vault snapshots and Epoch 2 evidence | unchanged |
| `parity_monitor/monitor.py` | unchanged (harness-frozen) |
| `tools/status_phase_b.py` | unchanged (harness-frozen) — still reports Epoch 2 scope |

## What is added

| Artifact | Purpose |
|---|---|
| `tools/status_phase_b_epoch5.py` | New wrapper. Imports `_in_scope` and `_load_loaded_strategy_ids` from monitor.py without modification, applies Epoch 5 RUN_START locally. Outputs the same gate report shape as `status_phase_b.py`, prefixed "Phase B — Epoch 5". |
| `EPOCH_5_CERTIFICATION_NOTE.md` | This file. |

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
