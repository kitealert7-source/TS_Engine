# OBSERVABILITY_HARDENING_PLAN

**Goal:** add the telemetry required to discriminate A (ENVIRONMENT) from C (H4) on future divergences. No logic changes. No signal changes. No strategy changes. No engine review.

**Verdict (A or B):**

> ## **A — Can be added without touching engine logic.**
> Every required field is reachable from public surfaces of `evaluate_bar` (`BarState`), the strategy's public DataFrame columns added by `prepare_indicators` / `apply_regime_model`, and the bar-loop / pipeline call-site DataFrame variables. All additions live in **runtime layer files** (`TS_Engine/live_runtime/bar_loop.py` + `shadow_journal.py`; `TS_Execution/src/pipeline.py` + `signal_journal.py`). No edits to `engine_dev/universal_research_engine/v1_5_9/evaluate_bar.py`, no edits to `engines/regime_state_machine.py`, no edits to `indicators/trend/kalman_regime.py`, no edits to any strategy file. One caveat is documented in §6 (the regime-cache key proper is reachable indirectly via input-window SHA-256 transitivity; capturing it directly would touch `engines/regime_state_machine.py` and is therefore not pursued here).

---

## 1.  What "no engine changes" means here

Files in scope (telemetry-layer, OK to edit when the plan is executed):

```
TS_Engine/live_runtime/bar_loop.py        — adds telemetry computation, write call
TS_Engine/live_runtime/shadow_journal.py  — adds new event types (BAR_TELEMETRY, EXIT_SIGNAL)
TS_Engine/live_runtime/telemetry.py       — NEW. snapshot helpers, hash function, no logic
TS_Execution/src/pipeline.py              — adds telemetry computation, write call
TS_Execution/src/signal_journal.py        — adds new event types if extending SignalJournal,
                                            else write to a parallel BarTelemetryJournal
TS_Execution/src/bar_telemetry.py         — NEW. snapshot helpers (mirror of TS_Engine version)
```

Files frozen (engine, indicator, strategy):

```
Trade_Scan/vault/snapshots/DR_BASELINE_2026_05_03_v1_5_8a/engine_dev/
    universal_research_engine/v1_5_9/evaluate_bar.py        FROZEN
Trade_Scan/engines/regime_state_machine.py                  FROZEN
Trade_Scan/indicators/trend/kalman_regime.py                FROZEN
Trade_Scan/indicators/**                                    FROZEN
Trade_Scan/strategies/62_TREND_IDX_5M_KALFLIP_*/strategy.py FROZEN
Trade_Scan/strategies/**                                    FROZEN
```

---

## 2.  Telemetry items, by category

### 2.1  Input snapshot

| Field | Source | Engine change? |
|---|---|:---:|
| `len(df)` | `len(df_eval)` after `prepare_indicators + apply_regime_model` | no |
| `first_bar_ts` | `df_eval.index[0]` formatted as ISO-8601 UTC | no |
| `last_closed_bar_ts` | `df_eval.index[-2]` (the bar `evaluate_bar` is invoked on) | no |
| `forming_bar_ts` | `df_eval.index[-1]` (canonical "forming" bar — never evaluated) | no |
| `ohlc_sha256` | `hashlib.sha256(df_eval[['open','high','low','close']].astype(np.float64).values.tobytes()).hexdigest()` | no |
| `ohlcv_sha256` | as above but include `volume` column if present (NAS100 OctaFX research CSV has `tick_volume` / live MT5 has `tick_volume`; column name normalization required, see §5) | no |
| `bar_count_post_warmup_increment` | bar counter for sanity (matches `execution_state.json bar_count`) | no |

**All reachable from already-bound local variables in `bar_loop.run_group_loop` (line 233 onwards) and `pipeline.run_on_bar_close`. `hashlib` and `numpy` are already imported in both runtimes.** No engine change.

### 2.2  Decision snapshot

| Field | Source | Engine change? |
|---|---|:---:|
| `in_pos_before` | snapshot of `state.in_pos` *before* the `evaluate_bar` call | no — `BarState.in_pos` is a public dataclass field |
| `in_pos_after` | snapshot of `state.in_pos` *after* the call | no |
| `direction_before` | `state.direction` (public field on `BarState`) | no |
| `direction_after` | as above, post-call | no |
| `entry_index` | `state.entry_index` (public field) | no |
| `bars_in_position` | derived: if `state.in_pos`, then `latest_closed_idx - state.entry_index`; else `None` | no — pure computation on public fields |
| `pending_entry_before` | shallow copy of `state.pending_entry` (already snapshotted at `bar_loop.py:255`) | no — already used for entry detection |
| `pending_entry_after` | `state.pending_entry` post-call | no — already used for entry detection |
| `entry_signal_fired` | `pending_entry_after is not None and pending_entry_after is not pending_entry_before` | no — derived |
| `exit_signal_fired` | derived: `in_pos_before and not in_pos_after` | no — pure transition detection |
| `exit_reason` | derived from BarState transition: `"resolve_exit_stop"` if `state.stop_price_active`-tripped (cannot disambiguate without engine review of `evaluate_bar`); use `"in_pos_transition"` as a generic label, optionally enrich from the bar's OHLC vs prior `state.stop_price_active` | no — but disambiguating SL vs strategy-exit requires reading bar OHLC against prior stop, which is a runtime-layer computation |
| `kalman_regime` | `df_eval['kalman_regime'].iloc[latest_closed_idx]` | no — public column added by `prepare_indicators` |
| `kalman_flip` | `df_eval['kalman_flip'].iloc[latest_closed_idx]` | no |
| `kalman_trend` | `df_eval['kalman_trend'].iloc[latest_closed_idx]` | no |
| `regime_market` | `df_eval['market_regime'].iloc[latest_closed_idx]` (added by `apply_regime_model`) | no |
| `regime_trend` | `df_eval['trend_regime'].iloc[latest_closed_idx]` | no |
| `regime_volatility` | `df_eval['volatility_regime'].iloc[latest_closed_idx]` | no |

**All reachable from `BarState` public attributes and `df_eval` public columns.** No engine change.

### 2.3  Cache snapshot

| Field | Source | Engine change? |
|---|---|:---:|
| `apply_regime_model_invoked` | always True per call (sentinel) | no |
| `regime_market_label` | output column of `apply_regime_model` (covered by 2.2) | no |
| `regime_id` | output column `regime_id` if present in `apply_regime_model` output (already in `REGIME … id=NN` execution log) | no — public column |
| `cache_file_path` | **NOT directly capturable** without modifying `regime_state_machine.py` | **engine change required to capture directly** |
| `cache_key_hash` | **NOT directly capturable** without modifying `regime_state_machine.py` | **engine change required to capture directly** |

**Workaround (no engine change):** the regime cache is a deterministic function of input bars (per `apply_regime_model`'s caching contract — content-addressed parquet files in `Trade_Scan/.cache/regime_cache/`). Therefore:

> `input_ohlc_sha256` (from §2.1) is a **cryptographic proxy** for the cache key. Two runtimes that journal identical `ohlc_sha256` necessarily compute identical cache keys (assuming `apply_regime_model`'s key function is itself deterministic on the input DataFrame, which §3 confirms is the case from the file-naming convention alone — content-addressed parquet).

This makes the cache-snapshot category **structurally answerable from the input snapshot alone, by transitivity**. We do not need to capture the cache file path directly to discriminate A vs C: if both runtimes' `ohlc_sha256` match at the divergent bar and they produce different `kalman_regime`, that is C; if they don't match, that is A. Direct cache-key capture would only become necessary if a future divergence shows matching `ohlc_sha256` but a suspicion of cache-state corruption (e.g. cache file was overwritten between read and use) — at which point the engine-change cost would be justified.

**Cache snapshot category therefore: reachable indirectly via input snapshot. No engine change.**

### 2.4  Exit journal (sidecar)

Currently TS_Engine sidecar's `shadow_journal.py` has `write_signal()` for entries and `write_marker()` for run lifecycle events; it does **not** journal exits. Bar-loop closure detects entry transitions via `pending_entry_before` / `pending_entry_after` (`bar_loop.py:255-263`) and emits `journal.write_signal()`. To add exit telemetry:

1. **No engine change required.** The transition `in_pos_before=True → in_pos_after=False` is fully detectable from `BarState` public attributes, identical pattern to existing entry-detection code.
2. Add a new event type `EXIT_SIGNAL` to `ShadowJournal` (parallel to existing entry events). Write per closure when an exit transition is detected. Fields:
    - `event_type = "EXIT_SIGNAL"`
    - `strategy_id`, `symbol`, `bar_ts` (existing)
    - `direction` (the closing direction = `state.direction` *before* the call; capture in `direction_before`)
    - `entry_bar_ts` (derived from `state.entry_index` before the transition; recoverable via the per-evaluation telemetry record's `last_closed_bar_ts` chain)
    - `bars_in_position` (derived per §2.2)
    - `exit_reason_proxy` — coarse classification: `"resolve_exit_intrabar_SL_or_TP"` if the closing bar's high/low crossed a known stop level (computable in runtime layer using `state.stop_price_active` snapshot taken before the call), else `"strategy_signal_exit"`. Disambiguating *which* mechanism fired more precisely requires reading `evaluate_bar`'s `resolve_exit` return value, which is currently not exposed — so the proxy is the most we can record without engine change.
3. Add a new event type `BAR_TELEMETRY` (one per evaluation, regardless of fire/no-fire) emitting the §2.1 + §2.2 fields. This is the new heavy-volume stream; ~12 records / minute / strategy at M5; ~30 fields each, JSON-encoded ~600 bytes/record → ~7 MB / day / strategy / runtime. Manageable.

---

## 3.  Per-runtime implementation map

### 3.1  TS_Engine sidecar (`live_runtime/`)

#### `bar_loop.py` (additions only — no edits to existing logic)

Insertion points (using current line numbers):

- **After line 233** (`df = _fetch_window(...)` returns):
  - capture `len(df)`, `df.index[0]`, `df.index[-2]`, `df.index[-1]`
- **After line 251** (`df_eval = apply_regime_model(df_eval)`):
  - compute `ohlc_sha256` over the full sliced window
  - capture regime output columns (`market_regime`, `trend_regime`, `volatility_regime`, `regime_id` if exposed)
- **Before line 257** (the `evaluate_bar` call):
  - snapshot `in_pos_before`, `direction_before`, `pending_entry_before` (already done for `pending_entry`), `stop_price_active_before`
- **After line 257**:
  - snapshot `in_pos_after`, `direction_after`, `pending_entry_after` (already done)
  - derive `entry_signal_fired`, `exit_signal_fired`, `bars_in_position`, `exit_reason_proxy`
  - read `kalman_regime`, `kalman_flip`, `kalman_trend` from `df_eval` at `latest_closed_idx`
  - call `journal.write_bar_telemetry(...)` with the assembled dict
  - if `exit_signal_fired`: call `journal.write_exit_signal(...)`
  - existing entry-fire logic (lines 263-306) is **unchanged**

No control-flow change. No call-order change. No state mutation outside `BarState` (which is unchanged). The added code is strictly read-and-emit.

#### `shadow_journal.py` (additions only)

Add two methods next to the existing `write_signal()`:

- `write_bar_telemetry(self, **fields) -> None`: append-only JSON line to `journal/bar_telemetry.jsonl`
- `write_exit_signal(self, **fields) -> None`: append-only JSON line; can write to existing `shadow_signal_journal.jsonl` with `event_type="EXIT_SIGNAL"`, *or* to a new `journal/exit_signal_journal.jsonl` (parity-monitor friendlier — see §4)

#### NEW: `live_runtime/telemetry.py`

A small helper module with:
- `def ohlc_sha256(df: pd.DataFrame) -> str` — fixed column order `['open','high','low','close']`, fixed dtype `float64`, hash via `hashlib.sha256` of the serialized bytes
- `def normalize_volume_column(df) -> str | None` — handles MT5's `tick_volume` vs `real_volume` vs CSV's `volume` (lookup, not normalization in-place)
- `def render_telemetry_record(...) -> dict` — assembles the per-bar telemetry dict from public sources

No logic, just data marshalling.

### 3.2  TS_Execution (`src/`)

#### `pipeline.py` (additions only)

Insertion points (matching the per-bar callback code path in `run_on_bar_close`):

- After bar fetch / DataFrame construction in the bar callback, before `prepare_indicators`:
  - capture `len(df)`, `df.index[0]`, `df.index[-2]`, `df.index[-1]`
- After `prepare_indicators` and `apply_regime_model`:
  - compute `ohlc_sha256`
- Around the `evaluate_bar` invocation (or its TS_Execution equivalent — `pipeline.run_on_bar_close` per CLAUDE.md does the same prepare-indicators-then-evaluate pattern):
  - capture pre / post `BarState` snapshots
  - emit `BAR_TELEMETRY` record
  - emit `EXIT_SIGNAL` record on `in_pos` transition

#### `signal_journal.py` (additions only — or NEW `bar_telemetry_journal.py`)

Same shape as the sidecar's: `write_bar_telemetry` + `write_exit_signal`. Can reuse `SignalJournal`'s append-only write infrastructure.

#### NEW: `src/bar_telemetry.py`

Same as `TS_Engine/live_runtime/telemetry.py`. **The two helper modules MUST share the exact same `ohlc_sha256` function** (same column order, same dtype, same byte-encoding) so cross-runtime hashes are comparable. Suggest: maintain one canonical implementation; copy verbatim into both repos with a comment marking which is the canonical source. (Or: place in `Trade_Scan/observability/` and import from both — but that touches `Trade_Scan/`, which is fine for a *new* file at a new path that the engine never reads.)

---

## 4.  Output journal layout (new files only)

```
TS_Engine/journal/
    bar_telemetry.jsonl              ← NEW. one record per bar evaluation per strategy
    exit_signal_journal.jsonl        ← NEW. sidecar's exit transitions
    shadow_signal_journal.jsonl      ← UNCHANGED. entries continue to be written here

TS_Execution/journal/
    bar_telemetry.jsonl              ← NEW. one record per bar evaluation per strategy
    exit_signal_journal.jsonl        ← NEW. parallel to sidecar (existing exit telemetry
                                       in shadow_trades.jsonl is shadow-trade-level, not
                                       BarState-level — this new file matches the sidecar
                                       structure for clean cross-runtime diff)
    SignalJournal.jsonl              ← UNCHANGED.
    ExecutedSignals.jsonl            ← UNCHANGED.
    shadow_trades.jsonl              ← UNCHANGED.
```

Per-record schema (canonical, shared across both runtimes):

```jsonc
{
  "event_type": "BAR_TELEMETRY",
  "runtime": "TS_Engine" | "TS_Execution",
  "run_id": "<runtime run id>",
  "strategy_id": "62_TREND_IDX_5M_KALFLIP_S01_V2_P15",
  "symbol": "NAS100",
  "timeframe": "5m",
  "written_utc": "2026-05-04T22:00:21Z",

  // Input snapshot
  "df_len": 1500,
  "first_bar_ts":        "2026-04-25 10:00 UTC",
  "last_closed_bar_ts":  "2026-05-04 22:00 UTC",
  "forming_bar_ts":      "2026-05-04 22:05 UTC",
  "ohlc_sha256":         "a1b2c3...",            // 64 hex chars

  // Decision snapshot
  "in_pos_before":       true,
  "in_pos_after":        false,
  "direction_before":     1,
  "direction_after":      0,
  "entry_index":         260,
  "bars_in_position":    20,
  "entry_signal_fired":  false,
  "exit_signal_fired":   true,
  "exit_reason_proxy":   "strategy_signal_exit",
  "stop_price_active":   27575.32,

  // Regime / indicator snapshot at last_closed_idx
  "kalman_regime":      -1,
  "kalman_flip":         false,
  "kalman_trend":        27672.0523,
  "regime_market":      "range_low_vol",
  "regime_trend":       -2,
  "regime_volatility":  -1,
  "regime_id":          32
}
```

Exit-signal record:

```jsonc
{
  "event_type":       "EXIT_SIGNAL",
  "runtime":          "TS_Engine",
  "run_id":           "<runtime run id>",
  "strategy_id":      "62_TREND_IDX_5M_KALFLIP_S01_V2_P15",
  "symbol":           "NAS100",
  "bar_ts":           "2026-05-04 22:00 UTC",
  "direction":        1,
  "entry_bar_ts":     "2026-05-04 20:20 UTC",
  "bars_in_position": 20,
  "exit_reason_proxy":"strategy_signal_exit",
  "stop_price_active":27575.32,
  "kalman_regime":    -1,
  "ohlc_sha256":      "a1b2c3..."
}
```

---

## 5.  Edge cases and gotchas

1. **Volume column naming.** Live MT5 returns `tick_volume` and (sometimes) `real_volume`. The research CSV uses `volume`. To make `ohlc_sha256` cross-runtime comparable, we hash **only OHLC** (drop volume) — this is the canonical decision. A separate `volume_sha256` can be journaled if/when broker-vs-CSV volume parity becomes a question, but is not needed for the H4 discrimination.
2. **Float-precision determinism.** Both runtimes use Python 3.10+, NumPy default float64. The `.values.tobytes()` cast on a float64 array is byte-for-byte deterministic on the same NumPy version. Cross-machine differences are theoretically possible (e.g., NumPy SIMD divergence) but vanishingly unlikely for raw OHLC values that come unmodified from MT5. Document the NumPy version that produced any given hash in the per-record metadata if forensic comparison ever fails over numeric noise.
3. **Forming-bar inclusion.** Both runtimes use the convention `df.iloc[-1]` = forming, `df.iloc[-2]` = latest closed. The `last_closed_bar_ts` and `forming_bar_ts` fields explicitly record both, so a future divergence can immediately tell whether one runtime had a different "forming" notion than the other (this was a hypothesized cause of divergence #6 — would now be falsifiable from the telemetry alone).
4. **Volume in hash.** Confirmed *excluded*. See gotcha 1.
5. **Time zones.** All `*_ts` fields written as ISO-8601 UTC ("`YYYY-MM-DD HH:MM UTC`" matches existing TS_Execution / TS_Engine convention). The `written_utc` field is wall-clock at the moment of write.
6. **Volume of telemetry data.** ~7 MB / day / strategy / runtime worst case. With 9 strategies × 2 runtimes = 18 streams × 7 MB = ~125 MB / day. Disk-budget acceptable. Append-only, no rotation needed for soak windows ≤ 30 days; archival rotation policy can match existing burnin log policy.
7. **Backpressure.** Bar-telemetry writes are O(bars_per_minute) — at most ~12/min/strategy at M5, easily within `f.write` + `f.flush` budget. No async / queue needed. Same fsync pattern as existing journals.
8. **Exit reason disambiguation.** The runtime layer can detect *that* an exit fired (in_pos transition) but not cleanly *why* (SL fill vs strategy signal exit) without inspecting `evaluate_bar`'s internal `resolve_exit` return. The plan settles for a coarse `exit_reason_proxy` derived from the closing bar's high/low vs the prior `stop_price_active`. For the H4 discrimination question, the *what-fired* (sidecar exited / didn't) is sufficient — we don't need the *why* to compare two runtimes' decisions.
9. **No mutation of `BarState`.** Snapshots use shallow `dict(...)` copies of dataclass `__dict__` for `pending_entry` (already done at line 255 of `bar_loop.py`). For other fields (`in_pos`, `direction`, `entry_index`), simple primitives — copy-by-value is automatic.

---

## 6.  What the plan deliberately does *not* try to capture

Three items would require touching the engine and are out of scope:

| Item | Why excluded |
|---|---|
| Internal Kalman posterior covariance `P` per bar | lives inside `kalman_regime()` function-local scope; would require either modifying the function to return it, or wrapping in a recording shim → strategy/indicator change |
| `apply_regime_model` cache file path / cache key hash directly | would require modifying `regime_state_machine.py` to log its hash function output → engine change |
| `evaluate_bar` internal `resolve_exit` reason code (SL vs TP vs strategy) | currently returned only via local `exit_reason` variable in `evaluate_bar`, not surfaced through `BarState` → engine change |

For the immediate H4 discrimination (next divergence on a parity-tracked strategy), these are not required. They become relevant only if input-SHA-256 parity is established and the divergence persists, in which case the deeper instrumentation can be authorized as a separate follow-up.

---

## 7.  Discriminator logic the new telemetry enables

Once both runtimes emit `BAR_TELEMETRY` records on every parity-tracked bar, future divergences resolve to one of three outcomes by mechanical inspection:

| Observation at the divergent bar | Verdict |
|---|---|
| `ohlc_sha256` differs between runtimes | **A — ENVIRONMENT** (input windows differed; not engine) |
| `ohlc_sha256` matches AND `regime_id` / cache-output columns differ | **B — CACHE** (same bars, regime cache state differed) |
| `ohlc_sha256` matches AND regime outputs match AND `BarState` decision (in_pos/direction/kalman_regime/exit_signal_fired) differs | **C — H4** (same input, same cache, different output → engine determinism review required) |

The plan's deliverable is the telemetry; the discriminator is the comparison script that consumes it. Author the comparison script alongside the telemetry implementation to avoid a second integration step.

---

## 8.  Activation sequence (when authorized)

> **Currently blocked.** Activation requires (i) a TS_Execution restart to load the new pipeline.py code, and (ii) sidecar startup to load the new bar_loop.py code. Both restarts are explicitly disallowed by the current directive. The plan is **deliverable only**; no implementation, no restarts.

When authorized, the activation sequence is:

1. Land code changes on a feature branch in both repos (TS_Engine, TS_Execution).
2. Run unit tests: hash determinism on a fixture DataFrame; round-trip JSON encoding.
3. Run a 30-bar dry replay using `kalman_replay.py` (already in place) to verify telemetry emission shape against a known-good input.
4. Stop TS_Execution + watchdog cleanly via `tools/stop_burnin.py`.
5. Pull main, redeploy.
6. Start TS_Execution via `tools/restart_burnin.py` (already-tested launcher).
7. Start sidecar via `python -m TS_Engine.live_runtime.runner` from `Trade_Scan` cwd.
8. Validate first 5 minutes of telemetry records present in both `bar_telemetry.jsonl` files.
9. Resume parity monitor.

---

## 9.  One-paragraph plan summary

Every field the H4 discriminator requires — input bar count, first/last/forming bar timestamps, OHLC SHA-256, BarState position fields, kalman_regime / kalman_flip / kalman_trend column values, apply_regime_model regime output columns, and sidecar exit transitions — is reachable from the public surfaces of `evaluate_bar` (via `BarState`), `prepare_indicators` (via DataFrame columns), and `apply_regime_model` (via DataFrame columns). No edits to the v1.5.9 engine, the regime model, the Kalman indicator, or any strategy file are required. The cache key proper is the only field that would require touching `regime_state_machine.py`, and it is structurally redundant with the input-window SHA-256 by content-addressing transitivity, so it is not pursued. **Verdict: A — can be added without touching engine logic.** Implementation lives in two new helper modules and additive blocks in `bar_loop.py` + `shadow_journal.py` (sidecar) and `pipeline.py` + `signal_journal.py` (execution), totaling roughly 250–350 lines of strictly read-and-emit code across both repos. Activation requires runtime restarts and is therefore deferred until explicitly authorized; the present deliverable is the plan only.
