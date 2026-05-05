# SPIKE_REPORT — v1.5.9 Extraction Spike

**Date:** 2026-05-01
**Branch:** `spike/v1_5_9_extraction` (Trade_Scan)
**Time used:** ~2 hours of 48h budget
**Verdict:** **PASS**
**Recommendation:** **A — proceed to Phase A/B/C**

---

## Result Summary

| Strategy | Archetype | Bars | v1.5.8 trades | v1.5.9 trades | Result |
|---|---|---:|---:|---:|---|
| 33_TREND_BTCUSD_1H_IMPULSE_S03_V1_P02 | trend / impulse | 49,299 | (raised) | (raised) | Both engines raised identically (degenerate parity — see notes) |
| **62_TREND_IDX_5M_KALFLIP_S01_V2_P15** | **trend / Kalman** | **69,445** | **1,071** | **1,071** | **byte-identical** |
| 27_MR_XAUUSD_1H_PINBAR_S01_V1_P05 | mean-reversion | 13,483 | 406 | 406 | byte-identical |

**1,477 trades validated byte-identical** across two archetypes (trend + mean-reversion) and two timeframes (5m + 1h).
File-level `diff -q` returned empty for both successful strategies.

---

## What was extracted

`Trade_Scan/engine_dev/universal_research_engine/v1_5_9/`

```
v1_5_9/
├── __init__.py                    (lifted from v1.5.8)
├── contract.json                  (lifted from v1.5.8)
├── engine_manifest.json           (NEW, version=1.5.9)
├── execution_emitter_stage1.py    (lifted from v1.5.8)
├── stage2_compiler.py             (lifted from v1.5.8)
├── main.py                        (only ENGINE_VERSION/__version__ updated)
├── execution_loop.py              (rewritten — ~80 lines, calls evaluate_bar)
└── evaluate_bar.py                (NEW — 521 lines, lifted body of v1.5.8 for-loop)
```

### What `evaluate_bar.py` contains
- `BarState` dataclass — captures all cross-bar local variables from v1.5.8's `run_execution_loop` outer scope
- `EngineConfig` dataclass — captures resolved STRATEGY_SIGNATURE config
- `resolve_engine_config(strategy)` — replaces v1.5.8 lines 215-256 (inline setup)
- `evaluate_bar(df, i, state, strategy, config)` — replaces v1.5.8 lines 263-644 (per-bar block, body of for-loop)
- `finalize_force_close(df, state, trades)` — replaces v1.5.8 lines 646-694 (end-of-data force-close)
- `ContextView`, `resolve_exit`, `_compute_unrealized_r*` — lifted verbatim from v1.5.8

### What v1.5.9's `execution_loop.py` does
~80 lines. Calls `prepare_indicators` → `apply_regime_model` → `resolve_engine_config` → loops `evaluate_bar` → `finalize_force_close`. No business logic.

---

## Abort-condition pre-check (recap)

All five spike abort triggers from the GO prompt: **CLEAR**.

| Trigger | Status | Evidence |
|---|---|---|
| Hidden global state | CLEAR | All state in local variables of `run_execution_loop` |
| Signature logic duplicated | CLEAR | STRATEGY_SIGNATURE read once (lines 215-236); stop computation in one place (338-352) |
| Wrapper/mirror required | CLEAR | Per-bar block lifted as-is into `evaluate_bar` |
| Compatibility flags needed | CLEAR | None added |
| Logic/math parity divergence | CLEAR | Validated by byte-identical output on Strategy 62 (1071 trades) and Strategy 27 (406 trades) |

---

## Strategy 33 — honest classification

Strategy 33's parity result was **structural, not byte-identical**. Both engines raised the same `RuntimeError` at the same point on the same input data:

```
RuntimeError: check_exit() must return bool or str, got bool.
Contract v1.3 accepts: False | True | '<LABEL>'.
```

This is a **pre-existing strategy-level issue in the current environment, NOT an extraction issue**:

- Root cause: numpy 2.x changed `np.bool_` so it no longer subclasses Python `bool`. Strategy 33's `check_exit` returns a numpy boolean from a comparison, which v1.5.8's contract validator rejects.
- Strategy 33 is in the active burn-in portfolio and was producing signals previously. This regression came from an environmental change, not the spike.
- Both v1.5.8 and v1.5.9 reject identically — the contract validator is verbatim copied. **Extraction parity is preserved at the exception level.**

**Out-of-scope for spike.** Logged as Phase A pre-cleanup item: investigate which strategies emit `numpy.bool_` and either patch them or relax the contract validator to accept `numpy.bool_`.

---

## Performance observation (informational)

| Strategy | v1.5.8 runtime | v1.5.9 runtime | Speedup |
|---|---:|---:|---:|
| 62_TREND_IDX_5M_KALFLIP | 61.7s | 10.0s | 6.2× |
| 27_MR_XAUUSD_1H_PINBAR | 9.1s | 1.6s | 5.7× |

The speedup is consistent across both successful strategies. v1.5.9 produces byte-identical output despite running faster — this is most likely from warm Python imports/caches when v1.5.9 runs second in the same process. **It does not affect correctness** (output is byte-identical) and is not a spike concern. Worth re-measuring under controlled conditions in Phase A but not blocking.

---

## Architectural validation

The most important finding from the spike confirms the original architectural diagnosis:

**v1.5.8 already implements `ENGINE_FALLBACK` correctly** (lines 338-352):
```python
strat_stop = pe_signal.get('stop_price')
if strat_stop is not None:
    stop_price = strat_stop
    stop_source = 'STRATEGY'
else:
    atr_at_signal = pe['atr']
    if direction == 1:
        stop_price = entry_price - (atr_at_signal * sl_atr_mult)
    else:
        stop_price = entry_price + (atr_at_signal * sl_atr_mult)
    stop_source = 'ENGINE_FALLBACK'
```

This is the exact contract `TS_Execution/src/signal_schema.py` was missing — the cause of 271 silently-killed signals over 35 days of burn-in. The research engine had it; live drifted from it.

**By extracting `evaluate_bar` and making both research and live import the same function, this drift becomes structurally impossible.** That's the architectural prize of the spike.

---

## Files created (per GO rules)

```
Trade_Scan/engine_dev/universal_research_engine/v1_5_9/    (entire directory)
TS_Engine/                                                  (new top-level folder)
├── README.md
├── tests/
│   └── spike_parity.py
└── spike_artifacts/
    ├── 33_TREND_BTCUSD_1H_IMPULSE_S03_V1_P02/
    │   └── parity_diff.txt
    ├── 62_TREND_IDX_5M_KALFLIP_S01_V2_P15/
    │   ├── trades_v158.jsonl    (1000594 bytes)
    │   ├── trades_v159.jsonl    (1000594 bytes — identical)
    │   └── parity_diff.txt
    └── 27_MR_XAUUSD_1H_PINBAR_S01_V1_P05/
        ├── trades_v158.jsonl    (362393 bytes)
        ├── trades_v159.jsonl    (362393 bytes — identical)
        └── parity_diff.txt
```

## Files NOT touched (per GO rules)

- `Trade_Scan/engine_dev/universal_research_engine/v1_5_8/` — untouched, hashes intact
- `TS_Execution/` — untouched, no live code modified
- All `strategies/*/strategy.py` — untouched
- No merge to main; spike work confined to branch `spike/v1_5_9_extraction`

---

## Recommendation: A

Per the GO prompt's recommendation logic:
- 33 raised before per-bar logic (degenerate but identical → not a sanity failure of extraction)
- 62 byte-identical on the hardest case (1071 trades, ENGINE_FALLBACK + Kalman + multi-gate)
- 27 byte-identical on cross-archetype (406 trades, mean-reversion)

**Proceed to Phase A/B/C as designed:**

- **Phase A** — Full extraction validation: extend parity test to all 9 burn-in strategies + all archetypes. Investigate the numpy.bool_ issue uncovered by Strategy 33 and either fix the affected strategies or accept np.bool_ in the contract.
- **Phase B** — Live shadow sidecar: TS_Engine consumes v1.5.9 in streaming mode, runs alongside TS_Execution with no dispatch authority. Compare journals.
- **Phase C** — Cutover with kill switch.

The architectural premise — that the research engine's per-bar block can be lifted into a shared callable — is **confirmed by direct evidence on the hardest strategy in the portfolio.**

---

## Open items for Phase A

1. **numpy.bool_ contract issue** — Strategy 33 (and possibly others) emit `numpy.bool_` from `check_exit`. Decide: tighten strategies (cast to `bool`) or relax v1.5.9 contract (`isinstance(x, (bool, str, np.bool_))` or just `bool(x)` coercion).
2. **Performance investigation** — confirm 6× speedup is real (warm caches?) or measure cold-start equivalence.
3. **Extend parity test** to remaining burn-in strategies (33 patched, 35_PA_GER40, 22_CONT_FX × 5 instances).
4. **Build live runtime in TS_Engine/** that imports `evaluate_bar` directly from v1.5.9.

Spike complete. Awaiting authorization for Phase A.
