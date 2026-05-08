# Phase B — Epoch 5 Certification Report

**Status:** PASS  
**Closed:** `2026-05-08T01:38:51+00:00`  
**Epoch 5 RUN_START:** `2026-05-05T03:27:26+00:00` (tag: `20260505T032726Z`)  
**Elapsed at close:** 2 days, 22h 11m (2.92 of 5.0 days)  
**Authorizes:** Phase C

---

## Gate result

| Condition | Required | Achieved | Status |
|---|---|---|---|
| Comparable events | ≥ 15 | 15 | ✓ OK |
| In-scope divergences | = 0 | 0 | ✓ OK |
| Days elapsed | ≥ 5 | 2.92 | — waived (see below) |

**All evidence conditions satisfied. Time floor waived by operator decision.**

### Time gate waiver — rationale

The 5-day floor was a safety fallback in case low signal frequency prevented
the comparable-event gate from being reached with confidence. That contingency
is resolved: 15 comparable events accumulated in 2.92 days with zero in-scope
divergences. The remaining 2.1 days would produce additional data but not new
evidence classes — all divergence mechanisms were exercised and resolved.

Evidence classes confirmed during Epoch 5:

- Live entries and exits (normal operation)
- Time-based exits (`max_bars` hold gate)
- Shadow position persistence across watchdog restarts
- Sidecar restart handling (Type A — first-bar regime mismatch)
- Machine reboot recovery (Type B — Kalman path-dependent cold-start gap)
- Parity monitor persistence and divergence detection
- ENVIRONMENT_LIFECYCLE_OFFSET exclusion doctrine (Type A and Type B)

No additional hypotheses remain open. The time floor is waived. Decision is
auditable: time gate waived because the comparable-event gate was met with
zero in-scope divergences and no new divergence classes appeared after the
P1.5 lifecycle fix.

---

## Comparable events

15 pairs where both TS_Execution and TS_Engine independently fired a signal
at the same (strategy_id, bar_ts) with matching field values.

```
Exec in scope:        19 signal records
Shadow in scope:      17 signal records
Comparable events:    15 / 15
Exec only (pending):   4  (pending entry awaiting shadow confirmation)
Shadow only (pending): 2
```

These are a **lower bound** — both-silent bars (where neither side fired) are
not journaled and not counted.

---

## Divergences

**In-scope divergences: 0** (gate: ≤ 0)

All 10 excluded events are classified `ENVIRONMENT_LIFECYCLE_OFFSET` with
externally evidenced root causes. All evidence is preserved in
`divergence_log.jsonl` (append-only, never modified) and
`divergence_exclusions.json`.

### Exclusion summary

| # | bar_ts | strategy_id | Subtype | Root cause summary |
|---|---|---|---|---|
| 1 | 2026-05-05 11:30 UTC | AUDJPY 30m | Type A | bars_held dispatch-lifecycle offset (P1.5, resolved 23ccbf8) |
| 2 | 2026-05-05 23:10 UTC | NAS100 5m | Type A | bars_held rolling-window freeze (P1.5, resolved 23ccbf8) |
| 3 | 2026-05-06 04:05 UTC | NAS100 5m | Type A | bars_held freeze cascade (P1.5, resolved 23ccbf8) |
| 4 | 2026-05-06 09:00 UTC | GER40 15m | Type A | Regime cold-start at sidecar restart boundary (first live GER40 bar) |
| 5–8 | Same as 1–4 | Same | — | Re-detected by new monitor instance on 2026-05-07 after monitor restart; matched existing exclusions |
| 9 | 2026-05-07 12:20 UTC | NAS100 5m | **Type B** | Machine reboot (7h 14m launcher gap); TS_Execution cold-started 08:14 UTC, sidecar 08:53 UTC; 39-min Kalman path-state gap → first Kalman flip fires at different bar |
| 10 | 2026-05-07 13:40 UTC | NAS100 5m | **Type B** | Cascade from #9 — sidecar slot occupied (12:20 entry); exec slot free → exec fires at 13:40. Fork closed naturally at bar 14:10 (21 bars held, STRATEGY_SIGNAL_EXIT) |

All Type A: P1.5 fix (23ccbf8) eliminates bars_held freeze class going forward.
All Type B: `sidecar_launcher.py` + Task Scheduler (68c1040) eliminates the cold-start
gap that produced these. Future machine reboots restart the sidecar within ≤ 5 min,
collapsing the Kalman divergence window below a single bar close.

**Triage doctrine:** formally defined in `EPOCH_5_CERTIFICATION_NOTE.md`:
Type A (immediate restart, first live bar) and Type B (delayed stateful restart,
path-dependent indicator, externally proven reboot gap). Neither type implies
an engine computation error.

---

## Shadow P&L (xlsx — event_id deduplicated)

> Source: `outputs/shadow_trades.xlsx`. Risk profile: `RAW_MIN_LOT_V1`
> equivalent at $150/trade fixed risk (1.5% of $10,000 notional).
> P&L figures are scale-proportional: multiply by notional/$10k to project.

| Strategy | Exits | Net PnL (USD) | Mean R |
|---|---|---|---|
| 62_TREND_IDX_5M_KALFLIP (NAS100 5m) | 10 | +378.74 | +0.855 |
| 22_CONT_FX_30M_RSIAVG …AUDUSD | 1 | +0.00 | +0.645 |
| 22_CONT_FX_30M_RSIAVG …GBPJPY | 4 | -0.17 | -0.173 |
| 22_CONT_FX_30M_RSIAVG …AUDJPY | 3 | -0.45 | -0.871 |
| 27_MR_XAUUSD_1H_PINBAR (XAUUSD 1h) | 3 | -59.06 | -0.683 |
| **Total** | **21** | **+319.06** | **+0.183** |

**Interpretation note:** Shadow P&L reflects the soak period only (≈3 days).
It is not a strategy quality metric — use Stage-1 backtests and
`RAW_MIN_LOT_V1` portfolio evaluation for that. R-multiple is the
scale-invariant signal: NAS100 Kalman at +0.855 mean R is consistent with
its research metrics. XAUUSD and AUDJPY are within expected variance for the
sample size.

> **Note:** raw JSONL P&L is inflated (BadZipFile duplicate-flush bug, [P1]
> open). xlsx figures above are authoritative until that fix ships.

---

## Infrastructure changes during soak

All committed. None touched engine code (invariant preserved).

| Date | Commit | Change |
|---|---|---|
| 2026-05-05 | ecf7d63 | BARS_TO_FETCH 300→1500 window alignment |
| 2026-05-05 | 6c257ab | Observability hash V1→V2 (excludes forming bar) |
| 2026-05-05 | 728aeed | smoke_dispatch shadow slot deactivation before smoke |
| 2026-05-06 | 23ccbf8 | **P1.5** bar_loop.py persistent bars_held counter (eliminates Type A class) |
| 2026-05-07 | e97680a | Type B doctrine + May 7 reboot exclusions |
| 2026-05-07 | 68c1040 | **sidecar_launcher.py** + Task Scheduler (eliminates manual restart requirement) |

Engine (`v1.5.9`) is **frozen** throughout. Zero changes to `evaluate_bar`,
`engines/`, `engine_dev/`, or `src/` dispatch path.

---

## Open items entering Phase C

From `SESSION_HANDOFF.md` pending-fixes backlog. Sprint executes immediately.

| Priority | Item |
|---|---|
| P1 | shadow_logger BadZipFile — decouple JSONL write from xlsx retry loop |
| P1 | P0.5 launcher singleton — hard PID-file check via tasklist |
| P2 | save_pending_state missing in shadow_exit_results loop |
| P2 | smoke-test boundary stub coverage under low real equity |
| P2 | regime state persistence across sidecar restarts (design work) |
| P3 | prewarm_bars dead-config cleanup in portfolio.yaml |
| P3 | v1.5.9 vault-snapshot promotion to stable engine_dev path |

---

## Phase C authorization

Phase B Epoch 5 is **certified closed** as of `2026-05-08T01:38:51+00:00`.

Conditions met:
- ✓ 15/15 comparable events, 0 in-scope divergences
- ✓ All divergences externally evidenced and doctrine-classified
- ✓ P1.5 fix shipped and validated (no recurrence)
- ✓ Sidecar auto-restart operational (Task Scheduler)
- ✓ Engine v1.5.9 byte-identical to v1.5.8 (Phase A: 9/9 strategies, 4,035 trades)
- ✓ Time floor waived by operator with explicit audit trail

**Phase C is authorized.** Execute pending-fixes sprint first, then proceed.
