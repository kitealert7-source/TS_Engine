# DIVERGENCE_22_10_ROOTCAUSE

**Subject:** divergence #6 at bar 2026-05-04 22:10 UTC, strategy `62_TREND_IDX_5M_KALFLIP_S01_V2_P15`
**Method:** archive-only forensics; no engine edits, no cache deletion, no live restarts
**Sources read:** sidecar `shadow_signal_journal.jsonl` + `sidecar.log`, execution `SignalJournal.jsonl` + `ExecutedSignals.jsonl` + `shadow_trades.jsonl` + `burnin_2026-05-04_0116.log`, `apply_regime_model` cache directory `Trade_Scan/.cache/regime_cache/`, parity monitor's `divergence_log.jsonl`

**Verdict (one of A / B / C):**

> ## **C — H4_ESCALATED.**
> Engine code review is required because the available archives **cannot affirmatively establish A or B**. Phase B's instrumentation does not record the per-bar 1500-bar input window or the per-bar regime-cache-key actually consumed by either runtime, so the comparison the question asks for is not directly answerable from the archive. The indirect evidence available is consistent with either an environmental cause (broker-tape skew at the NAS100 22:00 UTC daily-session boundary) **or** an engine determinism issue, and the burden-of-proof for clearing the engine has not been met. **C is the conservative call dictated by the evidence gap, not by a positive finding of H4.**

---

## 1.  What divergence #6 actually was

Re-reading the journals reveals divergence #6 is *not* a divergent decision at the 22:10 bar — it is a **downstream consequence of a divergent EXIT decision at the 22:00 bar**:

| Time (SVR/UTC) | Execution | Sidecar | Source |
|---|---|---|---|
| 20:20  ENTRY  | LONG entry, ref=27698.8, journaled | LONG entry, ref=27698.8, journaled | both journals |
| 20:20  bar close | 27698.8 (execution `entry_reference_price`) | 27698.8 (sidecar `bar_close`) | journal cross-check — **identical** |
| 22:00  EXIT  | `EXIT_SIGNAL_SHADOW … reason=shadow_exit … current=27655.30 bars=20` | NO EXIT (no journal entry) | burnin log, sidecar journal absence |
| 22:10  ENTRY | LONG re-entry fired (divergence #6 logged) | NO ENTRY (still in position from 20:20) | divergence log, sidecar journal absence |

Mechanism per `strategies/62_TREND_IDX_5M_KALFLIP/strategy.py`:
- `check_exit` returns True iff `_direction == 1` AND `kalman_regime == -1` AND `bars_held >= 20`.
- `entry_when_flat_only=True`: re-entry blocked while in-position.
- Therefore: if sidecar did not exit at 22:00, it cannot fire entry at 22:10.

**Question reduces to: why did execution's `kalman_regime` at 22:00 evaluate to −1 (triggering exit), while sidecar's evaluation evidently produced something other than −1 (no exit)?**

---

## 2.  Direct comparisons available from the archive

### Same bar count?

**Cannot determine from archive.** Neither runtime journals the size of its bar window per evaluation. Both call `mt5_reader.copy_rates(symbol, tf, 1500)` (sidecar `bar_loop.py:97-105`) or its TS_Execution equivalent; both *intend* to fetch 1500 bars; neither logs what was actually returned. No bar-count telemetry exists in either burnin log or shadow journal.

### Same timestamps?

**Cannot determine from archive.** No timestamp-list snapshot is captured per evaluation. Both runtimes use `df.iloc[-2]` as the latest closed bar (`bar_loop.py:124`, `bar_loop.py:242`); the timestamp is journaled only when a *signal fires*. Sidecar fired no signal at 22:00 (no exit signal journaled either, since exits are not journaled by the sidecar) and no signal at 22:10 — so no sidecar timestamp record exists for either bar.

### Same OHLCV values?

**Indirect evidence at one bar only.** At the 20:20 entry bar both runtimes journaled the same close (27698.8 — sidecar `shadow_signal_journal.jsonl` field `bar_close`; execution `SignalJournal.jsonl` field `entry_reference_price`). At every other bar in the window of interest, the sidecar journaled nothing.

### Same inclusion / exclusion of forming bar?

**Cannot determine from archive.** Both runtimes use the convention `df.iloc[-1]` = forming, `df.iloc[-2]` = latest closed. Whether the broker tape returned the same `iloc[-1]` to both at the moment of polling — i.e. whether one received the just-formed 22:10 bar while the other was still seeing the 22:05 close as the latest — is not recoverable from the archive. The execution log shows `BAR_DETECT NAS100 M5 wake_offset=+0ms detect_lag=11030ms` for the 22:10 evaluation (poll completed 11 s after wake-up at +20 s post-close = poll at 22:10:31 UTC). The sidecar's poll timing is not logged.

### Same regime-cache key / cache file?

**Cannot determine from archive directly. Indirect evidence is non-conclusive.**

- Cache directory `Trade_Scan/.cache/regime_cache/` contains content-addressed parquet files (filename = hash of input bars). Two runtimes computing on identical 1500-bar windows would hit the same cache entry; differing windows would create or hit different entries.
- Execution's `REGIME` log lines show a `id=NN` value per bar (apply_regime_model output `regime_id`):

  | Bar (SVR) | market | trend | vol | id |
  |---|---|---|---|---|
  | 21:50 | unstable_trend | −2 | −1 | 33 |
  | 21:55 | unstable_trend | −2 | −1 | 33 |
  | **22:00** | **range_low_vol** | **−2** | **−1** | **32** |
  | 22:05 | range_low_vol | −2 | −1 | 34 |
  | 22:10 | range_low_vol | −1 | −1 | 34 |

  These are execution's regime ids only. Sidecar does **not** journal regime ids — its only regime field per signal is the *string* `market_regime` (e.g. `range_high_vol`), present only on bars where a signal fired.
- The id at 22:00 is `32` — distinct from neighbouring `33` and `34` — meaning execution computed a *different* regime cache key at 22:00 than at the surrounding bars. This is consistent with the 22:00 bar being a session-boundary anomaly, but does not prove what cache key sidecar computed for the same bar.

**Conclusion: the regime-cache comparison the question asks for is not answerable from the archive. The cache-key telemetry exists only on the execution side and only for some bars.**

> **Side observation that limits the relevance of the cache to this divergence:** the strategy's `check_exit` reads `kalman_regime` (computed inside `prepare_indicators`, not cached) — it does *not* read any column produced by `apply_regime_model`. Even if the regime cache differed between runtimes, that alone could not directly cause the exit-vs-no-exit divergence. The cache could only matter indirectly if `apply_regime_model` mutates or re-orders the DataFrame in ways that subsequently change `kalman_regime`; review of `regime_state_machine.py` shows it appends new columns and does not modify the strategy-computed `kalman_regime` column.

---

## 3.  What the archive *does* tell us

1. **One direct cross-check passes**: at the 20:20 entry bar both runtimes saw the same bar close. This is the single moment where both journals expose a comparable bar-content field, and they agree.

2. **Execution's regime evaluation at 22:00**: `apply_regime_model` produced `market=range_low_vol, trend=−2, vol=−1, id=32`. The exit log confirms `bars=20` (exactly the `_MIN_HOLD_BARS` gate) and `reason=shadow_exit`, which by the strategy code's logic means `kalman_regime` evaluated to −1 with `_direction=1` and `bars_held=20`. Execution's input bars at 22:00 unambiguously produced `kalman_regime=−1`.

3. **Replay corroboration** (`KALMAN_STATE_INVESTIGATION.md`): two independent offline replays using the research CSV (`NAS100_OCTAFX_5m_2026_RESEARCH.csv`, OctaFX-derived) both produced exit at 22:00 / re-entry at 22:10 — i.e. they reproduced execution's behaviour exactly, **not** sidecar's. The replays' Kalman trend at 22:10 differed by 0.0001 between the 72 h and 12 h variants (effectively converged after 10 h of common bars), so the residual sidecar-vs-execution divergence at 22:00 cannot be attributed to filter state-age within plausible warmup-length differences.

4. **NAS100 daily-session boundary**: 22:00 UTC is the daily session-close moment for NAS100 on OctaFX. This is the highest-risk timing for broker-tape inconsistencies. Execution's `id=32` (singleton id at this bar, neighbours are 33/34) is consistent with — though does not prove — a one-off bar-window difference vs the neighbours.

---

## 4.  Three explanations, weighed against the evidence

### A.  ENVIRONMENT — input windows differed at 22:00

**Plausibility:** moderate-to-high.

**For:** 22:00 UTC is the NAS100 session boundary; broker-tape skew between two near-simultaneous polls is a known live-execution failure mode; execution's regime cache id `32` is anomalous vs neighbours `33`/`34`; the rate-limiter pressure at this moment (1262+ delays, 24/24 active per execution log L10406-10409) means each runtime's poll could legitimately have completed at a different effective moment.

**Against:** at the 20:20 bar both runtimes saw the same close, suggesting the broker tape was consistent for at least one nearby bar; the replay over the research CSV (an OctaFX-derived dataset, *very close* to live MT5 OctaFX tape) produces execution's behaviour, implying that "the live broker tape" should have produced execution's behaviour for both runtimes — **unless** sidecar's broker poll genuinely returned different bars.

**Verdict: cannot be confirmed without per-poll bar-window snapshots.**

### B.  CACHE — same bars, different cache state

**Plausibility:** low.

**For:** the regime cache directory is shared between runtimes; concurrent reads/writes during heavy traffic could in principle produce stale reads.

**Against:** the strategy's exit decision reads `kalman_regime` from `prepare_indicators`, not from `apply_regime_model`. `prepare_indicators` is **not cached**. Even if both runtimes hit different regime cache entries from `apply_regime_model`, that cannot change the `kalman_regime` value the strategy reads. For B to be the cause, `apply_regime_model` would have to mutate the input DataFrame in a way that affects `kalman_regime` — review of `regime_state_machine.py:apply_regime_model` (lines 38-103, top of file read) shows no such mutation: it appends new columns derived from `close` etc. and does not modify the OHLC4-based `kalman_regime` column.

**Verdict: structurally implausible for this specific divergence.**

### C.  H4 — same bars + same cache, different outputs

**Plausibility:** open.

**For:** Phase A's byte-identical reproduction was over batch backtests with fixed input data; it establishes engine determinism for that mode but does not exhaust live-execution failure modes (state corruption, threading, timing-sensitive code paths). The sidecar's lack of exit at 22:00 is fully explained by `kalman_regime != −1` *or* a state field (`_direction`, `entry_index`, `in_pos`) carrying an unexpected value into `check_exit`. Without sidecar telemetry, neither can be ruled out.

**Against:** the sidecar's `_direction` is an instance attribute set to 1 on entry and never explicitly reset by any code path I read; `bars_held` derives from `BarState.entry_index`, which the journal confirms was 20:20 → 20 bars at 22:00; `in_pos` is reset only on explicit exit. The simple H4 paths are not obviously triggered.

**Verdict: cannot be ruled out without engine review.**

---

## 5.  Why the verdict is C

The user's three options each require *positive* evidence:

| Option | Required evidence | Available? |
|---|---|---|
| A | input windows differ | indirect circumstantial only; not in archive |
| B | bars same, cache differs in a way that affects output | structurally implausible for this divergence |
| C | bars same, cache same, outputs differ → engine review | conservative default when A and B cannot be established |

The Phase B archive is **structurally insufficient** for A or B: the per-bar 1500-bar window snapshot, the per-bar `kalman_regime` value, and the per-bar regime-cache hash for the *sidecar* are not recorded. Without them, A cannot be confirmed even if it is the truth. B can be ruled out *structurally* on the grounds that the strategy's exit decision does not consume `apply_regime_model`'s output; this is a code-path argument, not an evidentiary one.

C is therefore the only option that is *defensible from the archive as it stands*. It does not assert that H4 is the likely cause — it asserts that **H4 cannot be ruled out** with the telemetry on disk, and that engine review (or telemetry hardening followed by re-test) is required before Phase C can proceed.

---

## 6.  Required follow-ups (out of scope of this audit)

The following actions are needed before divergence #6 can be definitively attributed. None of them violate the no-engine-edits / no-cache-deletion / no-live-restarts constraint of this investigation; they are *new* instrumentation, not modifications of the engine itself:

1. **Per-bar input-window snapshot** (lightweight): on each evaluation, log `len(df)`, `df.index[0]`, `df.index[-2]`, `df.index[-1]`, `sha256(df[['open','high','low','close']].values.tobytes())` to a sidecar-only and execution-only telemetry file. ~5 lines per runtime; would let A be proven or refuted definitively.
2. **Per-bar `kalman_regime` and `kalman_flip` capture** at the latest-closed-bar index, journaled per-evaluation by both runtimes. Already publicly exposed on the DataFrame; no engine edits needed.
3. **Per-bar regime-cache-key capture** (the hash that `apply_regime_model` keys on). Currently logged only as opaque `id=NN` for execution; would need extension.
4. **Sidecar exit telemetry**: sidecar currently journals only entries. Extending to also journal exits (via `BarState.in_pos` transition detection in the bar loop) would let presence asymmetry be debugged at the *exit* layer rather than inferred from the *next entry* layer.
5. **Engine determinism review** of `evaluate_bar` v1.5.9 for any code path that could produce different output given identical input under live (multi-threaded, multi-process) conditions — focus on shared-state in `apply_regime_model`'s cache layer, in `FilterStack.allow_trade`, and in the `Strategy._direction` field (an instance-level attribute that lives across bar evaluations on the same Strategy object).

---

## 7.  Process-state preservation (per directive)

| Constraint | Status |
|---|---|
| No engine edits | ✅ verified — no source files touched |
| No cache deletion | ✅ verified — `Trade_Scan/.cache/regime_cache/` untouched |
| No live restarts | ✅ verified — TS_Execution (PID 29588) and watchdog (PID 9792) running unaffected; sidecar (PID 34676) and parity monitor (PID 30128) remain stopped per Phase B termination doctrine |

## 8.  One-paragraph evidence summary

Divergence #6 at 22:10 UTC May 4 is the downstream consequence of a divergent **exit** decision at the preceding 22:00 UTC bar: execution's `kalman_regime` evaluated to −1 (triggering shadow exit at `bars=20`), the sidecar's evidently did not (no exit journaled, no re-entry possible at 22:10). At the 20:20 entry bar both runtimes journaled identical bar closes, establishing they shared a broker tape at that moment; at 22:00 they did not journal comparable telemetry and the comparison cannot be made from the archive. An independent offline replay using the research CSV reproduces execution's exit-at-22:00 / re-entry-at-22:10 pattern, implying the *intended-deterministic* engine behaviour matches execution; the sidecar's anomaly therefore lies *either* in a different broker-tape window pulled at the NAS100 22:00 UTC session boundary (ENVIRONMENT — A) *or* in an engine-state path that produced a different output despite the same input (H4 — C). The available archives lack the per-bar input-window and cache-key telemetry needed to discriminate these. B is structurally implausible because the strategy's exit decision does not consume `apply_regime_model`'s output. **The verdict is C — H4_ESCALATED, dictated by the evidence gap rather than by positive evidence of an engine defect: until per-bar telemetry hardening permits a direct A/B determination, engine determinism cannot be cleared and Phase C remains blocked.**
