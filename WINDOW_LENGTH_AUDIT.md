# WINDOW_LENGTH_AUDIT

**Question.** Why does TS_Engine fetch 1500 bars per evaluation while TS_Execution fetches 300? Trace the exact source for each value (config file, default constant, fallback path, strategy override).

**Method.** Read-only `Grep` across both repos; `git blame` on the lines that set the constants; `Grep` for any consumer of the YAML field that *appears* to expose the knob; comparison of in-source rationale comments between the two values. No code touched. No engine review.

**Verdict (one of A / B / C).**

> ## **B — Accidental config drift.**

The two values arose at different times, in different code, written by the same author, with **only one of them carrying any documented rationale**. The portfolio-level YAML knob that *appears* to govern the value on the execution side is **dead config: never read by any Python in either repo**. Both constants are otherwise hardcoded primary-path values — neither is a fallback or a degraded-fetch detour. The drift is not malicious nor architectural; it is the residue of two sources of truth that were never reconciled.

---

## 1.  TS_Engine sidecar — 1500 bars

**Single source of truth (constants):** `TS_Engine/live_runtime/bar_loop.py:88-92`

```python
# Bar window size — must be >= max indicator warmup across all strategies.
# 1500 bars is comfortable for: ATR(14), Hurst(100), regime model (~250),
# Kalman warmup. Adjust per-group if needed.
WARMUP_BAR_COUNT = 1500
LIVE_FETCH_COUNT = 1500
```

**Consumers:**
- `bar_loop.py:185` — `df_warm = _fetch_window(sym, tf, WARMUP_BAR_COUNT)` (startup warmup)
- `bar_loop.py:233` — `df = _fetch_window(sym, tf, LIVE_FETCH_COUNT)` (every live bar)

Both warmup and live fetch use the same value. Path is:

```
run_group_loop()
  └─ _fetch_window(sym, tf, 1500)
       └─ mt5_reader.copy_rates(symbol, tf_lower, 1500)
```

**No config-file path. No fallback. No strategy override.** The two integers are module-level literals.

**Rationale carried in source:** explicit. The comment names the four indicators that drove the choice (ATR/Hurst/regime/Kalman). The phrase *"must be >= max indicator warmup across all strategies"* is the design contract.

---

## 2.  TS_Execution — 300 bars

**Single source of truth (constant):** `TS_Execution/src/mt5_feed.py:148`

```python
BARS_TO_FETCH      = 300
POLL_INTERVAL_S    = 2.0    # seconds between polls in confirmation window
MARKET_CLOSED_S    = 30.0   # back-off interval when market appears closed
```

`git blame` on that line:

```
^e004da4 (kitealert7-source 2026-03-24 08:10:05 +0530 148) BARS_TO_FETCH = 300
```

`e004da4` is the **initial commit** of `TS_Execution`, message *"initial commit — TS_Execution execution bridge v1 (burn-in ready)"*. **The 300 value was set on day one of the repo and has not been touched since.**  No comment in the source. No commit-message rationale.

**Consumers:**
- `mt5_feed.py:230` — `rates = _fn(symbol, tf, 0, BARS_TO_FETCH)` (A2 probe path)
- `mt5_feed.py:254` — `rates = mt5.copy_rates_from_pos(symbol, tf, 0, BARS_TO_FETCH)` (legacy path)

Both code paths are *primary*; only one runs per process based on `MT5_LIMITER_OUTSIDE_LOCK` env flag. **Neither is a fallback.**  The constant flows uniformly into:

- Per-bar `fetch_bars()` calls during the live loop
- Pre-warm fetch at startup: `main.py:580` calls `fetch_bars(group.symbol, tf_int)` → 300 bars

**No strategy override.** Strategies expose no fetch-window field; the engine fetches once per group, not per strategy.

---

## 3.  The dead-config trap — `portfolio.yaml: prewarm_bars`

A field named `prewarm_bars: 300` appears in `TS_Execution/portfolio.yaml:16`, *and* is documented in `TS_Execution/README.md:47` as:

> `prewarm_bars` `300` History bars fetched at startup (matches `mt5_feed.BARS_TO_FETCH`)

**This field is never read by any Python file in either repo.**  Verified by exhaustive `Grep`:

| Search target | Repo | Hits |
|---|---|---|
| `prewarm_bars` | `TS_Execution/src/` | **0** |
| `prewarm_bars` | `TS_Execution` (full tree) | 2 — `portfolio.yaml`, `README.md` (data only) |
| `prewarm_bars` | `Trade_Scan` (full tree) | **0** |

**The README's wording — *"matches `mt5_feed.BARS_TO_FETCH`"* — is technically true (both equal 300) but functionally misleading.** Editing `portfolio.yaml` to set `prewarm_bars: 1500` would **change nothing**; the live fetch would still use 300 because the value comes from the hardcoded constant in `mt5_feed.py`, not from YAML.

This is the trap that makes the drift feel "intentional" on first read. It is not. The YAML field is decorative; the source-of-truth is the line in `mt5_feed.py` that has not been touched since 2026-03-24.

---

## 4.  Direct comparison

| Aspect | TS_Engine (1500) | TS_Execution (300) |
|---|---|---|
| Source location | `bar_loop.py:88-92` | `mt5_feed.py:148` |
| Stored as | `WARMUP_BAR_COUNT` + `LIVE_FETCH_COUNT` constants | `BARS_TO_FETCH` constant |
| Comment / rationale present | **Yes — names ATR(14), Hurst(100), regime model (~250), Kalman warmup** | None |
| Configurable from YAML | No | Appears to be (`prewarm_bars`) — but **YAML field is never read** |
| Fallback path | No — single primary path | No — two primary paths (env-flag selected), both use 300 |
| Strategy override | No | No |
| Consumed by | warmup + live | warmup + live |
| First set | When sidecar was authored (post-Phase A) | Initial commit 2026-03-24 |
| Same indicator stack served | Yes (9 strategies including 62_TREND_IDX_5M_KALFLIP) | Yes (same 9 strategies — single portfolio.yaml) |

The two runtimes feed the **same indicator pipeline** on the **same strategies** — the same `prepare_indicators` pass that runs ATR(14), Kalman, ADX(14), RSI smoothed, Hurst(100), and the same `apply_regime_model` that needs ~250 bars of history before its outputs are warmed. The rationale documented for 1500 in `bar_loop.py` applies *equally* to both runtimes by construction.

---

## 5.  Why each option was considered, and why B is the only fit

### A — Intentional design difference

Would require evidence that the two runtimes were specifically engineered to use different window sizes. **None exists.**
- No design doc (CLAUDE.md, README.md, TROUBLESHOOTING.md, EXECUTION_SPEC.md, or any in-tree `.md`) discusses or justifies a difference.
- No code comment in either repo references the other runtime's window size.
- The TS_Engine comment *"must be >= max indicator warmup across all strategies"* is a *shared* contract, not a sidecar-only one.
- The commit history shows TS_Execution's 300 was a day-one default with no discussion; TS_Engine's 1500 was authored later with explicit indicator-warmup reasoning.

If the difference were intentional, at least one of these would document the trade-off. None do. **A is not supported.**

### C — Runtime fallback path / degraded fetch

Would require that 300 represents a degraded mode — e.g. a smaller window invoked when the broker is rate-limited or returns short. **No such path exists.**
- `mt5_feed.fetch_bars()` has two code paths (`MT5_LIMITER_OUTSIDE_LOCK` env flag), and **both** request `BARS_TO_FETCH = 300` from MT5; neither is a reduced fallback.
- There is no retry loop that shrinks the window on failure.
- There is no per-call override that could push 300 below its nominal value.
- `_attempt_reconnect()` (line 267) exits on success/failure; the next `fetch_bars()` after reconnect still asks for 300.

300 is the **only** size TS_Execution ever requests. Not a fallback. **C is not supported.**

### B — Accidental config drift

Fits all observed evidence:
- 300 was set in TS_Execution's initial commit with no comment. The TS_Engine sidecar was authored later. The author chose 1500 *for the same indicator stack* with explicit rationale, and never updated TS_Execution to match.
- The `prewarm_bars: 300` YAML field + its README description suggest *someone intended* this to be a single-source-of-truth knob, but the wiring to a Python consumer was never finished. The YAML and the constant remain in numerical lock-step **only because they happen to share the same value, not because anything links them.**
- 300 bars is *below* the regime-model warmup margin documented in TS_Engine's source (~250 bars), leaving only ~50 bars of fully-warmed regime output. This is functionally suspect and would not be a deliberate choice given the rationale comment in `bar_loop.py`.
- 300 bars at 5-minute resolution = 25 hours of history. The Kalman filter's transient decay and Hurst(100)'s 100-bar look-back imply the filter posterior at any given live bar has integrated only the most recent ~25 hours; the sidecar's filter has integrated ~125 hours. **This is exactly the structural input-window mismatch surfaced by the new bar telemetry**, and the deterministic-replay finding from `KALMAN_STATE_INVESTIGATION.md` predicted that mismatched warmup history alters the Kalman regime decision on borderline bars.

The first telemetry record from the fresh-epoch run captures the drift directly — same closed bar, **`df_len = 1500` on the sidecar, `df_len = 300` on execution**, **different `ohlc_sha256`**. That observation matches exactly what an accidental drift would produce, and is structurally inconsistent with both A (which would have shown matching values for an intended-different reason) and C (which would have shown 300 only on rare degraded branches).

**B is the only option supported by evidence.**

---

## 6.  Implications (read-only — no action recommended in this audit)

1. **The window-length drift is the structural precondition for the H1 mechanism** demonstrated in `KALMAN_STATE_INVESTIGATION.md`. The Kalman filter is a deterministic function of input bars; different windows ⇒ different posterior trajectories ⇒ regime-decision differences on borderline bars.
2. **Aligning the two values would directly close the H4 question** — if both runtimes fetched 1500 bars and a divergence persisted, the fault would be unambiguously in engine code (since input SHA-256 would match). Conversely, if alignment makes divergences disappear, the engine is cleared.
3. **The `prewarm_bars` YAML field is currently a hazard, not a feature** — its presence implies a knob that doesn't exist. Either wire it to `BARS_TO_FETCH` or remove it.
4. **Neither value is necessarily "right"** for production. 1500 was chosen by the sidecar author with explicit rationale; 300 was chosen by the original TS_Execution author with no recorded reason. A reconciliation should re-derive the canonical value from the indicator stack's true warmup requirements (regime model ~250, Hurst 100, ATR 14, Kalman convergence ≥ a few hundred bars on M5) rather than pick one of the existing values by inheritance.

This audit makes no edits, recommends no specific value, does not change any constant, does not touch any YAML, and does not restart any process.

---

## 7.  Evidence trail

```
TS_Execution/src/mt5_feed.py:148            BARS_TO_FETCH = 300                    [initial commit e004da4, 2026-03-24, no comment]
TS_Execution/src/mt5_feed.py:230            rates = _fn(..., BARS_TO_FETCH)        [A2 probe path]
TS_Execution/src/mt5_feed.py:254            rates = mt5.copy_rates_from_pos(..., BARS_TO_FETCH)  [legacy path]
TS_Execution/src/main.py:580                rates = fetch_bars(group.symbol, tf_int)             [prewarm; uses BARS_TO_FETCH]
TS_Execution/portfolio.yaml:16              prewarm_bars: 300                       [DEAD — never read by any code]
TS_Execution/README.md:47                   "matches mt5_feed.BARS_TO_FETCH"        [doc claim; numerically true, structurally false]

TS_Engine/live_runtime/bar_loop.py:88-92    WARMUP_BAR_COUNT = LIVE_FETCH_COUNT = 1500
                                            [comment names ATR(14)/Hurst(100)/regime(~250)/Kalman warmup]
TS_Engine/live_runtime/bar_loop.py:185      df_warm = _fetch_window(sym, tf, WARMUP_BAR_COUNT)
TS_Engine/live_runtime/bar_loop.py:233      df = _fetch_window(sym, tf, LIVE_FETCH_COUNT)

TS_Engine/journal/bar_telemetry.jsonl       (first record, fresh epoch)
                                            sidecar:   df_len=1500  ohlc_sha256=66c2ebe5ce7ea61c…
TS_Execution/journal/bar_telemetry.jsonl    execution: df_len=300   ohlc_sha256=22945fa70c82904a…
                                            same closed bar (2026-05-05 06:00 UTC); same forming bar
```

---

## 8.  One-paragraph audit summary

`TS_Engine/live_runtime/bar_loop.py` defines `WARMUP_BAR_COUNT = LIVE_FETCH_COUNT = 1500` with an explicit in-source comment naming the four indicators (ATR(14), Hurst(100), regime model ~250, Kalman warmup) whose warmup requirement drove the value, and uses that single integer for both startup-warmup and per-bar live fetches; `TS_Execution/src/mt5_feed.py` defines `BARS_TO_FETCH = 300` as a bare constant committed on day one of the repo with no comment, no commit-message rationale, and no documented relationship to the indicator stack — yet feeds the same nine strategies and the same `apply_regime_model` pipeline as the sidecar. The portfolio-level YAML field `prewarm_bars: 300` looks like the canonical knob but is **never read** by any Python in either repo (verified by exhaustive grep), so its presence implies governance it does not provide. Neither value is a fallback (TS_Execution's two code paths both use 300 unconditionally) nor a strategy override (no per-strategy fetch-size field exists). The two values therefore arose independently, at different times, with only one of them justified in source, and the YAML mirror that *should* have linked them was never wired to a consumer — the textbook signature of accidental config drift. **Verdict: B.**
