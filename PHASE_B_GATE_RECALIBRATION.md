# Phase B Gate Recalibration

**Generated:** 2026-05-01T16:19:27Z  
**Tool:** `TS_Engine/tools/signal_density_audit.py`  
**Scope:** 9 burn-in strategies, vault + live journal data

---

## Audit Output

### Per-Strategy Signal Density (vault-implied)

| Strategy | Symbol | TF | Vault trades | Vault days | /day | /week | Live L30d | Live L90d |
|---|---|---|---|---|---|---|---|---|
| 62_TREND_IDX_5M_KALFLIP_S01_V2_P15 | NAS100 | M5 | 1,075 | 365 | **2.945** | 20.62 | 2 | 2 |
| 27_MR_XAUUSD_1H_PINBAR_S01_V1_P05 | XAUUSD | H1 | 451 | 820 | **0.550** | 3.85 | 7 | 7 |
| 35_PA_GER40_15M_DAYOC_S12_V1_P00 | GER40 | M15 | 395 | 819 | **0.482** | 3.38 | 0 | 0 |
| 33_TREND_BTCUSD_1H_IMPULSE_S03_V1_P02 | BTCUSD | H1 | 262 | 820 | **0.320** | 2.24 | 9 | 9 |
| 22_CONT_FX_30M_RSIAVG…P05 | GBPJPY | M30 | 324 | 1,904 | **0.170** | 1.19 | 3 | 3 |
| 22_CONT_FX_15M_RSIAVG…P01_GBPUSD | GBPUSD | M15 | 0 | — | **0.000** | — | 0 | 0 |
| 22_CONT_FX_15M_RSIAVG…P02_EURUSD | EURUSD | M15 | 0 | — | **0.000** | — | 0 | 0 |
| 22_CONT_FX_30M_RSIAVG…P06_AUDJPY | AUDJPY | M30 | 0 | — | **0.000** | — | 13 | 13 |
| 22_CONT_FX_30M_RSIAVG…P02_AUDUSD | AUDUSD | M30 | 0 | — | **0.000** | — | 7 | 7 |

**Note:** Four strategies (GBPUSD, EURUSD, AUDJPY, AUDUSD) have no vault CSV in DRY_RUN_VAULT, so vault-implied rates are 0. AUDJPY and AUDUSD nevertheless produced 13 + 7 = 20 live signals in the last 30 days.

---

### Portfolio Aggregate

| Metric | Value |
|---|---|
| Vault-implied portfolio rate | **4.467 signals/day** |
| Vault-implied portfolio rate | **31.27 signals/week** |
| Expected in 5 trading days (vault) | **22.3 signals** |
| Actual live last 30 days | **41 signals** |
| Actual live rate | **1.37 signals/day** |

---

### Time-to-N Projection at Vault Rate (4.47/day)

| Target | Calendar days | Trading days |
|---|---|---|
| 5 signals | 1.12 | 1.57 |
| 10 signals | 2.24 | 3.13 |
| 15 signals | 3.36 | 4.70 |
| **20 signals** | **4.48** | **6.27** |
| **30 signals** | **6.72** | **9.40** |
| 50 signals | 11.19 | 15.67 |

---

### Hour-of-Day Distribution (aggregated across all 9 strategies, vault)

The bulk of entries cluster in two windows:
- **09:00 UTC** — 480 trades (22.0%) — London open
- **16:00–18:00 UTC** — 389 trades combined (17.8%) — NY session / NAS100 active

The soak must span both windows to be representative. **This requires a minimum of 2–3 calendar days**, not 5.

### Weekday Distribution

Entries are nearly uniform Mon–Fri (18.9%–20.1% each), with near-zero Saturday (0.2%) and minimal Sunday (1.6%). No single weekday dominates; all 5 trading days must be covered.

---

## Gap Analysis — Is 30 Signals in 5 Days Realistic?

### The gap

| Rate source | Rate | Signals in 5 cal. days | Days to reach 30 |
|---|---|---|---|
| Vault-implied (5 strategies) | 4.47/day | 22.3 | **6.7 calendar days** |
| Actual live (30-day observed) | 1.37/day | 6.8 | **21.9 calendar days** |

**The 30-signal gate was set without running this audit.** At vault-implied rates, 30 signals requires 6.7 calendar days — already longer than the 5-day floor. At actual live rates, it requires ~22 days.

### Three structural reasons for the live vs vault gap

1. **Four strategies have no vault data.** GBPUSD and EURUSD fired 0 live signals in 30 days; AUDJPY and AUDUSD fired 20 combined. Their vault-implied rate is unknown (0 in audit, not 0 in reality). These strategies' live rates are unvalidated against any backtest baseline.

2. **NAS100 5m is the dominant driver.** At 2.945/day it supplies 65.9% of the vault-implied portfolio rate. Its live L30d shows only 2 signals — an order-of-magnitude gap. Either (a) Phase B1 only observed 5h20m of live operation, or (b) current market conditions differ from the 365-day vault window. Either way, projecting 2.945/day from a 1-year vault is optimistic.

3. **Phase B1 duration so far: 5h20m.** Zero signals is not informative — it's noise. At 4.47/day, the expected signal count in 5h20m is `4.47 × (5.33/24) = 0.99`. Zero signals in a <1-expected window is statistically unsurprising.

### What 30 signals was designed to guarantee

Enough aligned pairs across enough strategies that a 0-divergence result is not a statistical accident. With 9 strategies and realistic firing rates, that goal is met well below 30:

- At 15 aligned pairs from 5+ different strategies across 3+ trading days, a clean 0-divergence result is already meaningful.
- At 30 pairs, it is somewhat more meaningful but requires nearly twice the calendar time.
- The _shape_ of coverage (strategies, days, sessions) matters more than the raw count.

---

## Recommendation

### **B — Lower event threshold to 15. Keep 5-day minimum.**

**New gate: ≥ 5 calendar days AND ≥ 15 comparable events AND 0 unexplained divergences.**

**Comparable event definition (updated 2026-05-02):**
A comparable event is any bar where both runtimes evaluated the same strategy and agreed:
- Both emitted a signal (same direction) — observable, counted here
- Both produced no-signal — parity confirmed, but **not currently counted** (neither journal records silent evaluations; this count is a lower bound on true comparable events)
- Mismatch — divergence, counted against the gate

**Why B, not the others:**

| Option | Ruling |
|---|---|
| A — Keep 30 signals / 5 days | Rejected. Vault-implied rate predicts 22.3 in 5 days, not 30. Live rate predicts 6.8 in 5 days. The gate is miscalibrated and will not be met within a reasonable burn-in window. |
| **B — Lower threshold** | **Accepted. 15 signals is achievable at vault rate in 3.36 calendar days, so the 5-day time floor binds and provides the multi-day coverage requirement. Provides statistically meaningful parity evidence.** |
| C — Remove time dependency | Rejected. If signal rate is lower than expected, the soak could pass in 2 days via 15 lucky signals in a single session. Time gate ensures multi-day, multi-session coverage. |
| D — Portfolio too low-frequency | Rejected. The portfolio _does_ produce signals (41 in 30 days live). The issue is gate miscalibration, not portfolio inactivity. |

**Why 15 specifically:**

- At vault-implied rate (4.47 signal-events/day): 15 in 3.36 calendar days. Time gate (5 days) binds → covers both London and NY sessions across a full trading week.
- At live rate (1.37 signal-events/day): 15 in 10.9 calendar days. This is the conservative floor.
- 15 is not arbitrary: it is the signal count the 5-day time gate would deliver at vault-implied rate minus a 32% margin of safety (`22.3 × 0.68 ≈ 15`).
- Because both-silent evaluations are not counted, the true comparable-event rate is much higher than 4.47/day. The 15-event gate is deliberately conservative — we're counting a lower bound and requiring it to reach 15.

**Implementation changes:**

1. `status_phase_b.py`: renamed gate metric to `GATE_EVENTS = 15`, renamed all "aligned pairs" display labels to "comparable events", applied scope filter so only Epoch-2 bars count, added lower-bound disclosure note.
2. `PHASE_B_GATE_RECALIBRATION.md`: updated gate definition.

---

## Signal Coverage Expectation After Gate Recalibration

At vault-implied 4.47/day over 5 calendar days:
- Expected aligned pairs: **22** (above new gate of 15)
- Contributing strategies: 5 with vault data + AUDJPY/AUDUSD from live observation = **7 of 9**
- Session coverage: London open (09:00 UTC spike) and NY session (16:00–18:00 UTC) both represented within 5 days
- Weekday coverage: Mon–Fri all represented

The recalibrated gate is meaningfully challenging — it requires both time and volume — without being physically impossible.

---

## Action Items Before Phase B1 Restart

1. **Apply gate change**: update `GATE_SIGNALS = 15` in `status_phase_b.py`
2. **Clear divergence_log.jsonl**: the 6 pre-scope-filter divergences are already classified as harness artifact; remove them before restart so gate starts clean
3. **Confirm monitor.py scope filters in place**: PHASE_B1_RUN_START_UTC filter + strategy_id filter (both applied in previous session — verify still present)
4. **Restart Phase B1**: start TS_Engine sidecar + parity monitor; allow to run until gate met

No changes to engine logic, strategy logic, or TS_Execution required.
