# KALMAN_STATE_INVESTIGATION

**Subject strategy:** `62_TREND_IDX_5M_KALFLIP_S01_V2_P15`
**Question:** does state-age mismatch (H1) reproduce the live presence asymmetry, or is engine logic defect (H4) still possible?
**Method:** strict zero-edits offline replay using public engine surfaces only.
**Harness:** `TS_Engine/tools/kalman_replay.py`
**Engine:** `engine_dev.universal_research_engine.v1_5_9.evaluate_bar` (vault snapshot, byte-identical to v1.5.8)
**Bars:** `Anti_Gravity_DATA_ROOT/MASTER_DATA/NAS100_OCTAFX_MASTER/RESEARCH/NAS100_OCTAFX_5m_2026_RESEARCH.csv`
**Live window:** 2026-05-04 12:00 → 22:30 UTC (127 bars)

**Verdict (one of A / B / C):**

> ## **C — H4 still possible.**
> The replay reproduces the *mechanism* of state-age sensitivity at the 12:15 UTC bar, but with **reversed polarity** vs the live divergence, and the 22:10 UTC divergence is **not reproduced at all** because both replays' filters had converged by then. The live divergence pattern cannot be cleanly explained by warmup-window length alone. Engine code review is warranted before Phase C.

---

## 1.  Replay configuration

| Field | Replay A | Replay B |
|---|---|---|
| Warmup hours | 72 | 12 |
| Warmup window start (UTC) | 2026-05-01 12:00 | 2026-05-04 01:00 |
| Live window start (UTC)  | 2026-05-04 12:00 | 2026-05-04 12:00 |
| Live window end (UTC)    | 2026-05-04 22:30 | 2026-05-04 22:30 |
| Bars in window           | 400 | 259 |
| Live bars evaluated      | 127 | 127 |
| `evaluate_bar` source    | `engine_dev.universal_research_engine.v1_5_9.evaluate_bar` (vault) | same |
| `apply_regime_model`     | `engines.regime_state_machine.apply_regime_model` | same |
| `Strategy` class         | `strategies.62_TREND_IDX_5M_KALFLIP_S01_V2_P15.strategy.Strategy` | same |

A and B are byte-identical except for the 60-hour difference in preceding warmup bars.

---

## 2.  Headline result — fire / no-fire at the four target bars

| Bar (UTC) | Live engine (sidecar) | Live execution | A (72 h) | B (12 h) | A vs live engine | B vs live execution |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| 2026-05-04 12:15 | ABSENT  | PRESENT | **FIRES** | absent | mismatch | mismatch |
| 2026-05-04 15:00 | PRESENT | PRESENT | FIRES   | FIRES  | match    | match    |
| 2026-05-04 20:20 | PRESENT | PRESENT | FIRES   | FIRES  | match    | match    |
| 2026-05-04 22:10 | ABSENT  | PRESENT | **FIRES** | **FIRES** | **mismatch** | match |

Replays agree with each other on three of four bars (15:00, 20:20, 22:10) and disagree at 12:15 by exactly two M5 bars (B fires at 12:25 instead of 12:15).

**At neither divergence bar does the replay reproduce the live polarity.**

---

## 3.  Mechanism evidence — Kalman trend delta over time

Per-bar delta `A.kalman_trend − B.kalman_trend` at and around each target bar:

| Bar (UTC) | A trend | B trend | Δ (A − B) | A regime | B regime | Notes |
|---|---:|---:|---:|:---:|:---:|---|
| 12:10 | 27812.7978 | 27812.8443 | **−0.0466** | −1 | −1 | both flat-to-down |
| **12:15** | 27812.7981 | 27812.8424 | **−0.0443** | **+1** | **−1** | A flips, B does not |
| 12:20 | 27812.7668 | 27812.8089 | −0.0421 | −1 | −1 | A back to down |
| 12:25 | 27812.8137 | 27812.8538 | −0.0401 | +1 | **+1** | both flip — B fires here |
| 15:00 | 27740.6284 | 27740.6369 | −0.0085 | +1 | +1 | both fire |
| 20:20 | 27674.4330 | 27674.4333 | −0.0003 | +1 | +1 | both fire |
| 22:05 | 27671.8649 | 27671.8650 | −0.0001 | −1 | −1 | both flat |
| **22:10** | 27672.0522 | 27672.0523 | **−0.0001** | **+1** | **+1** | both fire — *no asymmetry to replay* |
| 22:15 | 27672.3644 | 27672.3645 | −0.0001 | +1 | +1 | both in position |

Two phenomena are visible:

1. **At 12:15, a 0.044-unit trend delta is sufficient to flip the regime decision.** A's `trend[12:15] (27812.7981) > trend[12:10] (27812.7978)` — slope positive — regime = +1 — flip detected — fires. B's `trend[12:15] (27812.8424) < trend[12:10] (27812.8443)` — slope negative — regime = −1 — no flip — does not fire. The state-age sensitivity of the regime decision on borderline bars is **real and reproducible**.

2. **By 22:10, the trend delta has decayed to 0.0001** — three orders of magnitude smaller than at 12:15. The Kalman filter (process_var=0.01, measurement_var=4.0, steady-state P ≈ 0.2) has converged within ~10 hours of common bars regardless of where each replay started. Both replays produce the same regime, the same flip, and the same signal.

---

## 4.  Why A's polarity is reversed vs live

The replay establishes that warmup length **changes regime decisions on borderline bars** (mechanism) but the **direction** of the change depends on which segment of price history each filter has integrated:

- Replay A's 72 h window starts 2026-05-01 12:00 — inside the May 1–3 downtrend; the filter has integrated significant downward bias before reaching the live window.
- Replay B's 12 h window starts 2026-05-04 01:00 — *after* the downtrend bottom; the filter starts cleaner and is biased less negative.

In live, the analogous anchoring is:
- Sidecar (continuous since 2026-05-01 16:29 UTC, 1500-bar warmup at startup) → filter integrates bars from approximately 2026-04-26 onward.
- Execution (restarted 2026-05-04 01:16 UTC, 1500-bar warmup at restart) → filter integrates bars from approximately 2026-04-28 onward.

The 2-day difference in starting bar between the two live runtimes is *small* compared to the 60-hour difference between A and B. Live's polarity at 12:15 went the opposite way (sidecar conservative, execution trigger-happy), implying that the small live state-age delta interacted with that specific April-26 vs April-28 anchor pair in a manner that **the 72 h vs 12 h test does not replicate**.

The mechanism is real. The polarity is anchor-dependent. **My test confirms the first and contradicts the second.**

---

## 5.  Why divergence #6 (22:10) is *not* reproduced

By 22:10 UTC, both replays have processed ~10 hours of identical live bars after their respective warmups ended. The Kalman filter's posterior at that point is governed almost entirely by recent observations, not by the differing warmup tails. The trend delta is 0.0001 — well below any threshold that could flip a regime decision. Both replays fire.

**This is the part that keeps H4 alive.** If the production sidecar and execution were both pulling the same 1500-bar window from MT5 at 22:10 May 4 (which is ~21 hours after the execution restart, by which point both windows would extend back to the same April 29 vicinity), they should have computed identical filter states and produced identical signals — exactly as my two replays do. But in production they did not — sidecar absent, execution present.

Three explanations remain on the table for divergence #6:
- **(i)** The two runtimes pulled subtly different 1500-bar windows from MT5 (e.g. one had the just-closed 22:10 bar, the other had only up to 22:05 or had the forming 22:15 bar present). This is an *environment* explanation, not engine.
- **(ii)** The regime model carried forward state across bars (or its on-disk cache had stale entries differing between runtimes). The harness reruns `apply_regime_model` per replay over a single-pass DataFrame; production's per-bar invocations might pick up cached values that drift between runtimes.
- **(iii)** A code-path branch in `evaluate_bar`, `apply_regime_model`, or one of the indicator functions produces non-deterministic output under conditions not exercised by either replay (H4).

The replay does not falsify H4. It also does not actively support it. But the inability of the H1 test to reproduce divergence #6 leaves H4 as a live possibility that must be addressed before Phase C.

---

## 6.  What the replay *does* establish

| Claim | Status |
|---|---|
| The Kalman filter is state-age sensitive on borderline bars | **Confirmed** (12:15 trend delta = 0.044 → regime flip) |
| 60 h of warmup-history difference is sufficient to flip a regime decision | **Confirmed** |
| 10 h of identical bars after warmup is enough for the filter to converge | **Confirmed** (delta decays from 0.044 → 0.0001) |
| The live divergence #5 polarity (sidecar absent, execution present) follows from a 72 h vs 12 h warmup pair anchored at the live runtimes' actual fetch points | **Falsified** (replay produces opposite polarity) |
| The live divergence #6 polarity follows from any warmup-length mismatch | **Falsified** (replays converge by 22:10; both fire) |
| The engine, given identical inputs and identical filter state, produces identical output | **Tested only weakly** — the two replays converge to identical outputs by 20:20+, which is consistent with engine determinism but does not exhaustively probe H4 |

---

## 7.  Recommendation derived from this evidence

The state-age mechanism is real but is **not sufficient** to explain the live divergence pattern. Recommendation A from `PHASE_B_FINAL_REPORT.md` (Phase C blocked pending Kalman state investigation) **stands**, with the following refinements:

1. **The Kalman filter's state-age sensitivity is confirmed** — bounded, decays in ~10 hours of common bars. This means restarted runtimes will exhibit transient divergences during the first few hours; this is operational reality, not engine bug.
2. **The 22:10 divergence is the unexplained residual.** Until reproduced or attributed:
   - Capture the exact 1500-bar windows each runtime pulled at 22:10 UTC May 4 (sidecar journal + execution journal) and compare bar-by-bar — if they differ, divergence #6 is **environment** (broker tape skew at session boundary), not engine.
   - If the windows are identical, the divergence cannot be explained by inputs or by my replay; that is the H4 case and `evaluate_bar` / `apply_regime_model` need a focused code review.
3. **Phase C remains blocked** until divergence #6 is attributed.
4. **No code changes were made.** Strict zero-edit constraint upheld throughout. Sidecar and parity monitor remain stopped per Phase B termination doctrine. TS_Execution + watchdog continue running unaffected.

---

## 8.  Artifacts

`TS_Engine/runtime_logs/archive_phase_b_final/kalman_investigation/`

```
REPLAY_A_72h.jsonl              127 per-bar records, 72 h warmup
REPLAY_B_12h.jsonl              127 per-bar records, 12 h warmup
kalman_replay_summary.json      target-bar comparison, summary metadata
```

Each JSONL record contains the publicly-exposed columns: `close`, `kalman_trend`, `kalman_regime`, `kalman_flip`, `atr`, `adx`, `rsi_smoothed`, `hurst`, plus `BarState` fields (`in_pos`, `direction`) and the per-bar `fired_this_bar` / `pending_entry` decision. No internal posterior covariance `P` was extracted — that would require a debug hook, which the strict-zero-edit constraint disallowed. The publicly-exposed `kalman_trend` array suffices to demonstrate state-age sensitivity at the 12:15 bar.

---

## 9.  One-paragraph evidence summary

The Kalman filter on this strategy is state-age sensitive on borderline bars: a 60-hour difference in preceding warmup history produces a 0.044-unit trend delta at 2026-05-04 12:15 UTC, which is sufficient to flip the regime decision (A: regime=+1 fires; B: regime=−1 does not). However, the polarity of that asymmetry is anchor-dependent and does not match the live divergence #5 polarity (live sidecar absent / execution present; replay A fires / B does not). By 2026-05-04 22:10 UTC — ten hours into a common bar tape — the trend delta has decayed three orders of magnitude to 0.0001 and both replays produce identical decisions; the live divergence #6 (sidecar absent / execution present) is **not reproduced**. State-age mismatch is therefore part of the picture but not a complete explanation. The unreproduced 22:10 divergence keeps H4 alive: until the actual 1500-bar windows the two live runtimes pulled at that bar are compared and shown to be identical (or shown to differ, attributing the divergence to environment), `evaluate_bar` and `apply_regime_model` cannot be cleared for Phase C.
