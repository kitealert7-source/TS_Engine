# INFRA_CAPABILITY_AUDIT

**Audit scope:** Can the existing pipeline run a deterministic offline replay of `62_TREND_IDX_5M_KALFLIP_S01_V2_P15` over the May 4 2026 NAS100 M5 window, with two independent runs differing only in initial filter state age (72 h vs 10–12 h history), in a way that would credibly distinguish **H1 (stateful filter convergence mismatch)** from **H4 (engine logic defect)**?

**Method:** read-only inspection of `Trade_Scan/`, `TS_Engine/`, `TS_Execution/`, and `Anti_Gravity_DATA_ROOT/`.  No code executed, no files modified.

**Verdict (one of A / B / C):**

> ## **B — Mostly ready.  Minor tooling missing.**

The engine, the strategy, the archived data, and the determinism guarantees are all in place. What is missing is a thin replay harness on top, plus a small per-bar logging hook to surface internal filter state. Neither requires engine logic changes.  Total estimated cost: ~150 lines of new harness script + ~15 lines of optional non-invasive logging.

---

## Capability matrix

| # | Capability | Status | Blocker for credibility? |
|---|---|---|---|
| 1 | Deterministic bar replay (ordered, byte-equivalent) | **Partial** | No — engine path exists; standalone replay harness does not |
| 2 | Controlled warmup windows (72 h vs 10–12 h) | **Partial** | No — hard-coded at 1500 bars, but workable by pre-slicing input |
| 3 | Single-bar `evaluate_bar()` entrypoint reused from live | **Present** | No |
| 4 | Per-bar Kalman state inspection (trend / P / threshold) | **Absent** | **Soft yes** — can confirm H1 from signals alone, but credibility against H4 is weakened without state telemetry |
| 5 | Deterministic repeatability (same input → same output) | **Present (with caveat)** | No |

---

## 1.  Deterministic bar replay — *Partial*

### What exists

- **`evaluate_bar()`** at `Trade_Scan\vault\snapshots\DR_BASELINE_2026_05_03_v1_5_8a\engine_dev\universal_research_engine\v1_5_9\evaluate_bar.py` — single-bar function that takes `(df, i, state: BarState, strategy, config)` and returns a decision dict.
- **`BarState`** dataclass (lines 194–216 of `evaluate_bar.py`) — explicit container for *all* trade-state plus session/partial/mutation fields. No hidden globals, no implicit state.
- **`_build_warmed_state()`** at `TS_Engine\live_runtime\bar_loop.py:112–129` — already loops `evaluate_bar()` from index 0 through `last_closed`, returning a warmed state. This *is* the deterministic replay primitive.
- **MT5 reader is read-only** and feeds `pd.DataFrame` shapes equivalent to what an archived CSV would produce after `build_dataframe()`.

### What is missing

- No standalone, parameterized "replay this CSV through this strategy with this warmup length" tool.  The Phase A 9/9-byte-identical reproduction was internal/ad-hoc — its driver script is **not present** in the current repos as a reusable entrypoint (verified — no `tools/reproduce*`, `tools/replay*`, or equivalent under either repo).
- The closest existing harness is the parity sidecar itself, which is online-only and not invocable against a static CSV.

### Cost to close

A new script under `TS_Engine\tools\` (or `Trade_Scan\tools\`) that:
1. loads the archived NAS100 5m CSV via the existing `data_access` loader
2. slices to a chosen `(prewarm_start, prewarm_end, live_end)` window
3. calls `strategy.prepare_indicators(df)` → `apply_regime_model(df)` once
4. instantiates `BarState()` and loops `evaluate_bar(df, i, state, strategy, cfg)` from `prewarm_start` through `live_end`, capturing returned dicts
5. writes one JSONL per run

Estimated **~150 lines, no engine changes**, calls only public surfaces of `evaluate_bar` and `bar_loop._build_warmed_state` (already exported). This is harness, not engine.

---

## 2.  Controlled warmup windows — *Partial*

### Findings

- Live warmup is **hard-coded** at `WARMUP_BAR_COUNT = 1500` (`TS_Engine\live_runtime\bar_loop.py:78`); `TS_Execution\src\pipeline.py:run_prewarm` likewise consumes the entire `rates` buffer it is handed without exposing a length parameter.
- 72 h of M5 = 864 bars; 10–12 h of M5 = 120–144 bars.  Both fit comfortably in the 1500-bar window the live system already pulls.
- Because the replay harness (capability 1) builds its DataFrame from a pre-sliced CSV, **the warmup length is controlled by what we hand it, not by an engine parameter**. We do not need to change `run_prewarm` or `WARMUP_BAR_COUNT` — we just feed two different DataFrames.

### Cost to close

Zero engine changes.  Two pre-sliced CSVs (one with 72 h preceding the live window, one with 12 h preceding), both ending at the same point — produced by a one-time pandas slice from the master CSV. This is data prep, not code.

---

## 3.  Single-bar `evaluate_bar()` reused from live — *Present*

### Evidence

- TS_Engine sidecar imports `evaluate_bar` directly:
  ```python
  # TS_Engine\live_runtime\bar_loop.py:40-45
  from engine_dev.universal_research_engine.v1_5_9.evaluate_bar import (
      BarState, EngineConfig, evaluate_bar, resolve_engine_config,
  )
  ```
- Per-bar invocation: `bar_loop.py:127–128` (warmup loop) and `bar_loop.py:257–258` (live loop) both call the same `evaluate_bar(...)`.
- TS_Execution's `pipeline.run_on_bar_close` runs the same `prepare_indicators` → `apply_regime_model` → `check_entry` → `validate` flow on the same `ContextView` contract.

The replay harness will invoke **the same function symbol from the same module** that produced the divergent signals. There is no "test engine" path that could mask H4. **This is the strongest determinism guarantee available** — same code, same Python, same numpy, just different DataFrames.

### Caveat — engine version

CLAUDE.md states *"Engine is FROZEN at v1.5.3"* but the actual sidecar imports `engine_dev.universal_research_engine.v1_5_9`.  The acceptance criterion stamped at the top of the v1.5.9 file is *"Backtest output byte-identical to v1.5.8 for every strategy"* (lines 22–25), and the sidecar/execution have been running on it through the soak. **This is a doc-vs-code drift, not a determinism gap** — but it should be reconciled in the documentation pass post-investigation.  It does not affect this audit.

---

## 4.  Per-bar Kalman state inspection — *Absent*

### What the strategy carries today

`Trade_Scan\indicators\trend\kalman_regime.py` produces a per-bar Kalman estimate with these state variables:
- `trend[i]` — filtered estimate (one per bar)
- `P` — posterior variance (scalar, advanced bar-to-bar)
- `K` — Kalman gain (scalar, recomputed each bar)
- Output exposed to the strategy: `trend` array + `regime` array (∈ {−1, +1})

Filter parameters are locked in the strategy file:
- `_KALMAN_PROCESS_VAR = 0.01`
- `_KALMAN_MEASUREMENT_VAR = 4.0`

`check_entry` reads only `kalman_flip` (bool) and `regime` (int) — the **internal posterior variance and the unfiltered trend value never reach a journal**.

### Why this matters for credibility

H1 says "different state ages produce different filter posteriors that straddle the flip threshold."  We can falsify or confirm H1 from **signals alone**: if Replay A (72 h warmup) and Replay B (12 h warmup) produce different fire/no-fire decisions on bars 12:15 / 22:10 UTC May 4, the asymmetry is reproduced and H1 is supported.

But if we want to **distinguish H1 from H2 (warmup-window-insufficiency-only)** and to *show why* the filter disagreed, we need the per-bar `trend` and `P` traces.  Without them, an "H1 confirmed" finding rests on a black-box signal comparison.

### Cost to close

Two non-invasive options:

**Option α — strategy-side debug list (preferred, ~10 lines):**
- Add optional `self._debug_state: list[dict] | None = None` to the strategy class.
- Inside `prepare_indicators`, append `{bar_idx, trend, P, regime, kalman_flip}` per bar when the list is initialized.
- Replay harness sets the list, drains it after the run, writes JSONL.
- **No effect on signal output** — `_debug_state` is read-only telemetry.

**Option β — wrap `kalman_regime()` in a logging proxy (~15 lines):**
- Replay harness imports `kalman_regime`, wraps it with a recording shim before passing to the strategy.
- Zero strategy file edits.

Either is "logging hook only," not engine logic change. Option α is cleaner.

---

## 5.  Deterministic repeatability — *Present (with caveat)*

### Confirmed

| Concern | Status | Evidence |
|---|---|---|
| RNG in live path | **No** | Only RNG in pipeline.py is `np.random.default_rng(seed=42)` at `pipeline.py:352`, used inside the *startup synthetic-OHLC smoke harness* — never on live or replayed bars. |
| Dict / set iteration order | **Stable** | Both repos target Python 3.10+; insertion order is guaranteed. |
| Wall-clock dependence | **None observed** | No `datetime.now()` reads in `evaluate_bar`, `kalman_regime`, or strategy `check_entry`. Bar timestamps come from the input DataFrame. |
| Floating-point precision | **IEEE 754 standard** | No precision settings touched; numpy default. |
| Engine version stability | **v1.5.9, declared byte-identical to v1.5.8** | Acceptance line 22–25 of `evaluate_bar.py`. |

### Caveat

Determinism is guaranteed *for identical Python / numpy / pandas binaries*. If the harness is run on a host with a different numpy build than the sidecar/execution were running, transcendental-function differences could in principle produce a 1-ulp change in the Kalman estimate that crosses a threshold.  **Mitigation:** run the harness on the same host as the live system, in the same venv. (Trivial — no work.)

---

## What "no code changes" means in practice

The original investigation directive said *"no code changes."*  Strict reading: zero edits to any tracked file. The audit shows this is **achievable for the engine, but the test loses one observability dimension** (Kalman internal state) under that constraint. Specifically:

| Constraint level | What we can answer |
|---|---|
| **Strict — zero edits anywhere** | Can the asymmetry be reproduced with two warmup lengths? (yes/no, signal-only) — sufficient to confirm H1 vs H4 directionally; insufficient to *show the mechanism*. |
| **Relaxed — harness scripts only, no engine edits** | Same as above; cleaner output organization. *(This is the no-engine-code-changes interpretation.)* |
| **Relaxed — harness + non-invasive logging hook on the strategy** | Full picture: signals + per-bar `(trend, P)` traces showing exactly where the filter posteriors diverge. Cleanly distinguishes H1 from H2 from H4. |

**Recommendation embedded in this audit:** adopt the third interpretation.  The "logging hook only" addition is ~10 lines, has zero effect on signal output, and is reversible (drop the attribute when done).  Without it, an H1-confirmed verdict will rest on signals alone and a Phase C reviewer could reasonably ask "but did the filter actually disagree, or did something else?"  With it, the answer is on disk.

If the strict-zero-edits constraint must hold, the test is **still runnable and still credible at the H1-vs-H4 level**, just with a thinner evidence base.  Classification stays B either way.

---

## Concrete checklist to move from B to "ready to run"

1. **Pre-slice the master CSV** (`Anti_Gravity_DATA_ROOT\MASTER_DATA\NAS100_OCTAFX_MASTER\RESEARCH\NAS100_OCTAFX_5m_2026_RESEARCH.csv`) into:
   - `replay_input_72h.csv` — 72 h preceding 2026-05-04 12:15 UTC + the live window through 2026-05-05 00:00 UTC
   - `replay_input_12h.csv` — same live window, only 12 h preceding
2. **Author replay harness** (`TS_Engine\tools\kalman_replay.py`, ~150 LOC). Uses public surfaces only:
   - `engine_dev.universal_research_engine.v1_5_9.evaluate_bar.{evaluate_bar, BarState, EngineConfig, resolve_engine_config}`
   - `Trade_Scan` strategy module + `apply_regime_model`
   - Optional: drains `strategy._debug_state` if present (forward-compatible with hook).
3. **(Optional, recommended)** Add the ~10-line `_debug_state` capture to `Trade_Scan\strategies\62_TREND_IDX_5M_KALFLIP_S01_V2_P15\strategy.py`. Mark with a clear `# DEBUG-INVESTIGATION:` comment so it can be reverted.
4. **Run twice**, write `replay_72h.jsonl` and `replay_12h.jsonl` (and optional `kalman_state_72h.jsonl` / `kalman_state_12h.jsonl`).
5. **Compare** the four bars of interest (post-restart NAS100 Kalman events 12:15 / 15:00 / 20:20 / 22:10 UTC May 4) across the two runs.  Authoritative inputs to `KALMAN_STATE_INVESTIGATION.md`.

Steps 1, 2, 4, 5 require zero engine edits.  Step 3 is the only contemplated source change and is purely additive logging — it can be skipped without invalidating the test, only narrowing its diagnostic resolution.

---

## Verdict in one paragraph

The engine entrypoint, archived data, determinism guarantees, and warmed-state primitive are all present and reused directly from the live path.  The two missing pieces — a parameterized replay driver and a non-invasive Kalman state logger — are test-harness work, not engine work.  The investigation as defined is credible to run; the question is only whether to take the strict "zero edits anywhere" path (signals-only, still confirms H1 vs H4 directionally) or the recommended "harness + logging hook" path (full mechanistic evidence).  Either way, the classification is **B — Mostly ready, minor tooling missing**.  Phase C remains blocked.  No live restarts.  No engine logic changes contemplated.
