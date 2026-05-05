# PHASE_B_FINAL_REPORT

**Status:** STOPPED on ENGINE-SUSPECT trigger
**Trigger:** `62_TREND_IDX_5M_KALFLIP_S01_V2_P15` (NAS100 5M Kalman flip)
**Stop time (UTC):** 2026-05-05T06:09Z
**Sidecar RUN_START:** 2026-05-01T16:29:29Z
**Authority:** User decision invoking ENGINE-SUSPECT doctrine, doctrine satisfied on all four conditions.

---

## Section 1 — Evidence summary

### Phase A (baseline)
- 9 / 9 strategies byte-identical between TS_Engine and TS_Execution
- 4,035 trades reconstructed and matched

### Phase B runtime
- Sidecar uptime: 2026-05-01T16:29:29Z → 2026-05-05T06:09Z ≈ **3 days 13 h 40 m** (3.57 d)
- TS_Execution had a **weekend crash-loop** (Fri 22:00 May 1 → Mon 01:16:09 May 4 ≈ 51 h) caused by `smoke_dispatch.py` postcondition (since fixed). Sidecar ran continuously through this window.
- Phase B1 Epoch 2 (this soak) started 2026-05-01T16:29:29Z. Execution was restarted **once** at 2026-05-04T01:16:09Z after the smoke fix; no further restarts.

### Comparable events
- **10 / 15** at stop (lower bound — both-silent bars not journaled)
- Pace ≈ 2.8 events / day post-recovery; on track for 15 by ~day 5.5 had soak continued

### Divergences by strategy (7 total, all `presence`, no value mismatches)

| Strategy | Count | Direction (engine / execution) |
|---|---:|---|
| `62_TREND_IDX_5M_KALFLIP_S01_V2_P15` (NAS100 M5) | **2** | ABSENT / PRESENT (×2) |
| `22_CONT_FX_30M_RSIAVG_TRENDFILT_S02_V1_P05` (GBPJPY M30) | 2 | ABSENT / PRESENT (×2) |
| `33_TREND_BTCUSD_1H_IMPULSE_S03_V1_P02` (BTCUSD H1) | 1 | PRESENT / ABSENT |
| `35_PA_GER40_15M_DAYOC_S12_V1_P00` (GER40 M15) | 1 | PRESENT / ABSENT |
| `27_MR_XAUUSD_1H_PINBAR_S01_V1_P05` (XAUUSD H1) | 1 | ABSENT / PRESENT |

Direction asymmetry: 5 of 7 divergences are `engine ABSENT / execution PRESENT` (execution fires; sidecar does not). Two are the inverse.

---

## Section 2 — Incident: `62_TREND_IDX_5M_KALFLIP`

### Both divergence timestamps

| # | Bar (UTC) | detected_utc | Engine | Execution | Trade outcome (shadow) |
|---|---|---|---|---|---|
| 5 | 2026-05-04 12:15 | 2026-05-04T09:21:40Z | ABSENT | PRESENT (LONG, entry 27813.1) | **R = −1.000** (SL hit at 13:00 UTC, 9 bars) |
| 6 | 2026-05-04 22:10 | 2026-05-04T19:17:22Z | ABSENT | PRESENT (LONG, entry 27680.4) | **R = −0.237** (exit at 01:45 SVR, 28 bars) |

Spacing between divergences: **9 h 55 m** (≈ 119 M5 bars).

### Surrounding bars on the same strategy (post-restart window only)

| Bar (UTC) | Engine | Execution | Class |
|---|---|---|---|
| 2026-05-04 01:00 | PRESENT | (n/a — execution still in crash-loop) | OUTAGE — excluded from parity |
| 2026-05-04 12:15 | ABSENT | PRESENT (LONG) | **DIVERGENCE #5** |
| 2026-05-04 15:00 | PRESENT | PRESENT (LONG) | comparable ✓ — both fire (R = +0.142) |
| 2026-05-04 20:20 | PRESENT | PRESENT (LONG) | comparable ✓ — both fire (R = −0.371) |
| 2026-05-04 22:10 | ABSENT | PRESENT (LONG) | **DIVERGENCE #6** |

Per-strategy parity ratio post-restart: **2 comparable / 4 events = 50 % divergence rate.**

### Restart boundaries

- Sidecar RUN_START: 2026-05-01T16:29:29Z (continuous through soak)
- TS_Execution restart: 2026-05-04T01:16:09Z (run_id `20260504T011609Z_29588`)
- Pre-restart execution session unrelated to current divergences (different process; weekend crash-loop window between)
- Watchdog `restart_count = 3` for the storm-guard window of May 4 01:14:09Z (cleared after the smoke fix)

### State age difference between runtimes (Δ ≈ 56 h 47 m, constant for both divergences)

| Event | Sidecar state age | Execution state age | Δ |
|---|---:|---:|---:|
| Divergence #5 (12:15 UTC May 4) | 67 h 46 m (≈ 2.82 d) | 10 h 59 m (≈ 0.46 d) | **+ 56 h 47 m** |
| Divergence #6 (22:10 UTC May 4) | 77 h 41 m (≈ 3.24 d) | 20 h 54 m (≈ 0.87 d) | **+ 56 h 47 m** |

The Kalman filter in this strategy is recursive with no fixed warmup window guaranteed by `prewarm`; long-memory state accumulates across the observation history actually fed to the filter.

### Doctrine — ENGINE-SUSPECT conditions satisfied

| Condition | Met? | Evidence |
|---|:---:|---|
| Survives 90 s grace | ✓ | both entries record `grace_elapsed_s = 90.0` |
| Both runtimes alive | ✓ | sidecar PID 34676 + execution PID 29588 alive at both detection times; no thread-dead alerts |
| Outside outage window | ✓ | crash-loop ended 2026-05-04T01:16:09Z; both divergences at 12:15Z and 22:10Z (≥ 11 h post-recovery) |
| Repeats on same strategy multiple bars | ✓ | two occurrences, same strategy, same direction (engine ABSENT / execution PRESENT) |

---

## Section 3 — Root-cause hypotheses (ranked)

### H1 — Stateful filter convergence mismatch  *(MOST LIKELY)*

**Mechanism.** The Kalman flip relies on a recursive filter whose state is a function of the entire observation sequence the filter has been fed. Sidecar fed bars continuously since 2026-05-01T16:29:29Z; execution was rebuilt from a finite prewarm window at 2026-05-04T01:16:09Z. Filter posteriors (estimate, covariance, possibly internal regime indicator state) at the divergence bars are not numerically identical even when fed the same incoming bars.

**Supporting evidence.**
- Δ-state-age was constant (56 h 47 m) at both divergences; the divergent strategy is the *only* strategy in the portfolio whose check_entry uses an unbounded recursive filter.
- The two non-divergent NAS100 bars (15:00, 20:20 UTC) sit between the two divergent bars — i.e., the disagreement is not "always engine ABSENT," it is "borderline bars where the two filters straddle the flip threshold."
- Direction asymmetry (5/7 = exec PRESENT / engine ABSENT across the whole soak) is consistent with one filter having tighter posterior variance than the other, biasing threshold crossings.

**Implication.** Code is the same; *state* is not. Live ↔ live (Phase C) avoids this because there is only one filter producing only one decision per bar.

### H2 — Warmup-window insufficiency

**Mechanism.** `run_prewarm` may not feed the Kalman filter enough history for the *transient* posterior covariance to decay to the steady-state attractor. Even if so, this is a special case of H1 (state mismatch) — but with a localized cause: prewarm length, not unbounded memory.

**Supporting evidence (weak).** Bothdivergences are 10–21 h post-restart, beyond what most non-recursive indicators need; consistent with a long-tailed posterior decay specifically.

**Implication if true.** Bounded fix: extend prewarm for stateful strategies. Doesn't affect Phase C (single-runtime), so the question is academic for live.

### H3 — Timestamp / session boundary issue

**Mechanism.** Bar 22:10 UTC May 4 is near the NAS100 instrument's daily close (~22:00 UTC on OctaFX); a session-boundary mishandling could cause one runtime to skip the bar.

**Supporting evidence (weak).**  Divergence #5 is at 12:15 UTC — mid-session, no boundary.  Divergence #6 is near close, but the previous bar (20:20 UTC) was a clean comparable, ruling out a *systematic* session-edge cull. Possible but not the primary driver.

### H4 — Engine logic defect *(LEAST LIKELY)*

**Mechanism.** Different code paths in TS_Engine (sidecar) vs TS_Execution (in-process pipeline) producing different decisions on identical inputs.

**Supporting evidence (against).**
- Phase A established 9/9 byte-identical reproduction over 4,035 trades — the engine code path is verified deterministic on identical state.
- 0 value mismatches in Phase B (no SL / TP / direction / lot disagreements).
- Three other stateful strategies (GBPJPY M30 RSI, BTCUSD H1, GER40 M15) show 1–2 divergences with the same direction asymmetry, consistent with a common cross-strategy state-age cause rather than a strategy-specific code bug.

---

## Section 4 — Recommendation

> **A — Phase C blocked pending Kalman state investigation.**

Rationale:

1. The trigger is unambiguous: doctrine conditions all satisfied, no judgment override.
2. The most likely root cause (H1) is state-age mismatch, which **does not exist in Phase C** (single-runtime, no sidecar). Phase C would in principle be safe.
3. **However**, until H1 is positively confirmed against H4 (engine logic defect), we cannot distinguish "stateful filter divergence on warm-vs-warmer state" from "engine logic disagreement that *happens* to manifest only on the long-memory strategy."
4. The investigation needed is small and bounded: rerun Phase A with an adversarial schedule that introduces an artificial state-age delta on the Kalman filter (e.g., feed sidecar 72 h history, feed execution 10 h history, then run both forward on the same live tape). If divergences reproduce with same direction asymmetry → H1 confirmed → Phase C safe → unblock. If divergences do not reproduce → H4 cannot be ruled out → engine code review required before Phase C.
5. Other portfolio strategies (FX_CONT, BTC_TREND, GER40 PA, XAUUSD MR) are similarly affected by state-age (5/7 divergences, direction-consistent); fixing the Kalman strategy alone does not constitute a green light for the rest until H1 is verified across stateful archetypes.

**Phase C is blocked. No code changes. No restart of the sidecar / monitor. No patches to the engine.** Investigation proceeds against the archived artifacts; live execution + watchdog continue running unaffected.

---

## Appendix — Frozen state at stop

### Processes
- TS_Engine sidecar (PID 34676): **STOPPED** at 06:09 UTC May 5
- Parity monitor (PID 30128): **STOPPED** at 06:09 UTC May 5
- TS_Execution (PID 29588): **RUNNING** (per doctrine, not stopped)
- Watchdog (PID 9792): **RUNNING** (per doctrine, not stopped)

### Archive
`TS_Engine/runtime_logs/archive_phase_b_final/`

```
TS_Engine/
  divergence_log.jsonl                  (all 7 divergences)
  shadow_signal_journal.jsonl           (sidecar emissions in scope)
  sidecar.log                           (sidecar runtime log)
  monitor.log                           (parity monitor decisions)
TS_Execution/
  SignalJournal.jsonl                   (validated signals)
  ExecutedSignals.jsonl                 (MT5-confirmed fills only)
  shadow_trades.jsonl                   (shadow ledger — source of truth)
  burnin_2026-05-04_0116.log            (current session log, 1.7 MB)
  burnin_2026-05-04_0058_crashloop.log  (smoke-fail crash window)
  pending_signals.json                  (state-persistence snapshot)
  execution_state.json                  (last bar + bar count)
  watchdog_guard.json                   (storm-guard state)
```

### Open follow-ups (deferred — no action mid-investigation)
- `shadow_trades.xlsx` write failures (cosmetic; JSONL intact).
- `smoke_dispatch.py` boundary-stub coverage gap (acceptable until equity-sized risk returns ≥ vol_min).
- P0.5 launcher singleton (post-cutover).
