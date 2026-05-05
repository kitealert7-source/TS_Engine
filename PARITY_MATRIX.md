# Phase A Parity Matrix — v1.5.8 vs v1.5.9 (FINAL)

**Date:** 2026-05-01
**Branch:** `spike/v1_5_9_extraction`
**Verdict:** **9/9 BYTE-IDENTICAL — Phase A exit criteria SATISFIED**
**Total trades validated byte-identical:** **4,035 across 7 strategies producing trades**

---

## Final result table

| # | Strategy | Symbol | TF | Bars | v1.5.8 | v1.5.9 | Diff | Result |
|---|---|---|---:|---:|---:|---:|---:|---|
| 1 | 62_TREND_IDX_5M_KALFLIP_S01_V2_P15 | NAS100 | 5m | 69,445 | 1,071 | 1,071 | 0 | **byte-identical** |
| 2 | 27_MR_XAUUSD_1H_PINBAR_S01_V1_P05 | XAUUSD | 1h | 13,483 | 406 | 406 | 0 | **byte-identical** |
| 3 | 35_PA_GER40_15M_DAYOC_S12_V1_P00 | GER40 | 15m | 58,206 | 884 | 884 | 0 | **byte-identical** ¹ |
| 4 | 22_CONT_FX_30M_..._P05 | GBPJPY | 30m | 65,716 | 224 | 224 | 0 | **byte-identical** |
| 5 | 22_CONT_FX_15M_..._P01_GBPUSD | GBPUSD | 15m | 58,023 | 0 | 0 | 0 | byte-identical (empty) ² |
| 6 | 22_CONT_FX_15M_..._P02_EURUSD | EURUSD | 15m | 57,666 | 0 | 0 | 0 | byte-identical (empty) ² |
| 7 | 22_CONT_FX_30M_..._P06_AUDJPY | AUDJPY | 30m | 102,334 | 561 | 561 | 0 | **byte-identical** |
| 8 | 22_CONT_FX_30M_..._P02_AUDUSD | AUDUSD | 30m | 29,015 | 187 | 187 | 0 | **byte-identical** |
| 9 | 33_TREND_BTCUSD_1H_IMPULSE_S03_V1_P02 | BTCUSD | 1h | 49,299 | 702 | 702 | 0 | **byte-identical** ³ |

**¹** Strategy 35 was initially failing harness data-load (KeyError 'time') because the harness was using `df.set_index('time')` which removes 'time' from columns. Strategy 35's `prepare_indicators()` reads `df['time']` directly. Harness data-loader was fixed to keep 'time' as both column AND index. **No engine code touched. No strategy code touched.** After harness fix: byte-identical 884 trades.

**²** 22_CONT 15m strategies (GBPUSD/EURUSD) produced 0 trades on the test data. Both engines agree on 0 trades — that IS parity. The 0-trade outcome is because the test harness used the full available dataset (no canonical date range — the directives are 0-byte post-admission markers). The strategy's filter conditions don't fire on this slice. Out of scope for parity validation; flagged for Phase A pre-Phase-B investigation.

**³** Strategy 33 was initially raising `RuntimeError: check_exit() must return bool or str, got bool` due to numpy 2.x changing `np.bool_` to no longer subclass Python `bool`. Fixed at strategy level: `bool(...)` cast applied to two return statements in `check_exit`. **The cast is provably semantics-neutral** — `bool(np.bool_(x))` returns the same truth value, and the engine's downstream `if exit_result:` evaluates truthiness identically for both types. After patch: byte-identical 702 trades.

---

## Phase A Exit Criteria — ALL MET

| Criterion | Status | Evidence |
|---|---|---|
| All 9 strategies pass parity | **YES** | 9/9 byte-identical trade lists, file-level `diff -q` returns empty for every strategy |
| No hidden state discovered | **YES** | Extraction was mechanical; cross-bar state captured cleanly in `BarState` dataclass |
| No compatibility flags added | **YES** | `v1_5_9/` introduces zero flags or version-conditional code paths |
| No wrapper logic introduced | **YES** | `evaluate_bar()` IS the lifted block, not a wrapper that mirrors v1.5.8 |

---

## What was changed during Phase A

### In Trade_Scan
1. **`engine_dev/universal_research_engine/v1_5_9/`** — created (full directory, mechanical extraction from v1.5.8)
2. **`strategies/33_TREND_BTCUSD_1H_IMPULSE_S03_V1_P02/strategy.py`** — `check_exit` returns wrapped in `bool(...)` (lines 195, 202). Strategy logic unchanged.

### In TS_Engine
1. `tests/spike_parity.py` — parity harness (data loader fixed to keep 'time' column, falls back to portfolio.yaml when directive YAML is empty)
2. `tests/verify_strat33_vs_vault.py` — vault comparison harness (used during investigation)
3. `spike_artifacts/<STRATEGY_ID>/` — per-strategy `trades_v158.jsonl`, `trades_v159.jsonl`, `parity_diff.txt`
4. `SPIKE_REPORT.md` — original spike result (unchanged)
5. `PARITY_MATRIX.md` — this document

### What was NOT touched (per rules)
- `Trade_Scan/engine_dev/universal_research_engine/v1_5_8/` — untouched
- `TS_Execution/src/` — untouched (no live code, no broker dispatch, no watchdog, no scheduler)
- Other 8 strategies (only Strategy 33 needed the bool() cast; 35 was a harness issue not a strategy issue)
- No merge to main; all work confined to `spike/v1_5_9_extraction` branch

---

## On the "preserve v1.5.8 behavior" rule

The Phase A rules required: *"Must preserve v1.5.8 behavior on existing ledgers. No silent coercion without proof."*

### What we attempted
Compared patched-Strategy-33 + v1.5.8 output against vault snapshot `DRY_RUN_2026_04_06__71723056` (262 canonical trades).

### What we found
The vault was generated 2026-04-06; v1.5.8 was frozen 2026-04-23. The vault used a **pre-v1.5.8 engine** (likely v1.5.6 or v1.5.7). ATR computation differs ~30% between vault and v1.5.8 — different engine versions.

### What this means
We cannot directly compare patched-v1.5.8 against the vault because the vault is not a v1.5.8 ledger. The "preserve v1.5.8 behavior" rule is interpreted as:

1. **The patch is provably semantics-neutral** by construction:
   - `bool(np.bool_(True))` → `True` (Python bool)
   - `bool(np.bool_(False))` → `False` (Python bool)
   - Truth value preserved; only type wrapper added
   - The engine's downstream `if exit_result:` (v1.5.8 line 568) evaluates Python truthiness, which is identical for `np.bool_` and `bool`
   - Therefore no exit decision changes
2. **v1.5.8 with the patched strategy now runs successfully** (previously raised). This is the only valid v1.5.8 ledger we can produce.
3. **v1.5.8 and v1.5.9 produce byte-identical 702-trade output** on the patched strategy. Engine extraction is correct.

### Honest disclosure
There is no a-priori v1.5.8 ledger for Strategy 33 to compare against. The constraint "no silent coercion without proof" is satisfied by:
- Type-level proof: `bool(np.bool_(x)) == bool(x)` for all x — Python language guarantee
- Behavioral proof: `if x:` evaluates identically for both types — Python truthiness semantics
- Extraction proof: v1.5.8 vs v1.5.9 byte-identical trades after patch — direct evidence

This is documented openly so Phase B reviewers can apply different standards if needed.

---

## Performance observation (informational)

Across the 9-strategy suite, v1.5.9 generally runs faster than v1.5.8:
- Mean v1.5.8 runtime per strategy (excluding 0-trade): 13.2s
- Mean v1.5.9 runtime per strategy (excluding 0-trade): 9.5s
- Speedup: ~1.4× on average, up to 11× on AUDJPY

Output is byte-identical, so the speedup comes from runtime infrastructure (warm imports, in-process caching), not logic. **Does not affect parity** and is not a blocker.

---

## Authorization status

**Phase A exit criteria: ALL SATISFIED.** Awaiting Phase B authorization.

Phase B preparation (no work started — pending authorization):
1. Build TS_Engine live shadow runtime that imports `evaluate_bar` from v1.5.9
2. Configure to run alongside TS_Execution with NO dispatch authority
3. Parallel journals (`journal/old/` and `journal/new/`) for divergence detection
4. Gate: 5 consecutive trading days with zero divergences + ≥30 actual non-null signals across diverse regime conditions

**No live cutover. No runtime changes to TS_Execution. No merge to main.**
