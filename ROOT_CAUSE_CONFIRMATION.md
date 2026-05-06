# ROOT_CAUSE_CONFIRMATION — bars_in_position Rolling-Window Bug

**Classification:** ENGINE-HARD-LIVE-MODE  
**Confirmed:** 2026-05-06  
**Reporter:** Session investigation (context chain from 2026-05-05 ENVIRONMENT_LIFECYCLE_OFFSET triage)  
**Status:** Root cause confirmed, patch designed, awaiting implementation approval

---

## 1. Symptom

`bar_telemetry.jsonl` for any position entered during the live sidecar loop shows:

```
"bars_in_position": 0    ← on every bar, indefinitely
```

For strategy `62_TREND_IDX_5M_KALFLIP_S01_V2_P15` (`_MIN_HOLD_BARS = 20`),
the shadow position entered at "2026-05-05 11:30 UTC" never fired a signal exit
over 9+ hours of market activity. Downstream: repeated presence divergences at
"2026-05-05 23:10 UTC" and "2026-05-06 04:05 UTC" where TS_Execution journaled
new entries after correct exits but the sidecar did not.

---

## 2. Code evidence — three-location trace

### Location 1: `bar_loop.py` line 286 — fixed `i` on every live call

```python
# bar_loop.py, LIVE LOOP (run_group_loop)
latest_closed_idx = len(df) - 2   # LIVE_FETCH_COUNT = 1500 → always 1498
```

`evaluate_bar` is called with `i = latest_closed_idx = 1498` on **every single
bar evaluation** throughout the sidecar's lifetime. The value never changes.

### Location 2: `evaluate_bar.py` lines 362–366 — entry_index set from `i`

```python
# evaluate_bar.py, pending entry execution branch
if direction_allowed:
    state.direction   = pe_direction
    state.in_pos      = True
    state.entry_index = i           # ← set to 1498 when position opens
    state.entry_price = row.get('open', row['close'])
    ...
    return None
```

`entry_index` is only ever written inside this block — once, when the position
opens. It is never written again while the position remains open. On a live-loop
entry: `state.entry_index = 1498`.

### Location 3: `evaluate_bar.py` line 338 — bars_held computed from both

```python
ctx_ns = SimpleNamespace(
    ...
    bars_held=(i - state.entry_index) if state.in_pos else 0,
    ...
)
```

`ctx.bars_held` — the value every strategy reads in `check_entry` and
`check_exit` — is `i - state.entry_index`.

**In backtest:** `i` increments from 0 → N; `entry_index` = entry bar's
absolute index; `bars_held` advances correctly.

**In live sidecar:** `i = 1498` always; `entry_index = 1498` (set on entry);
`bars_held = 1498 − 1498 = 0` **every bar, forever**.

---

## 3. The rolling-window mechanism

Each live bar:

```
fetch fresh 1500-bar window  →  df[-1] = forming bar (excluded)
                                df[-2] = latest closed bar → index 1498
```

The window slides forward by 1 bar each period. The absolute position of the
entry bar in the window therefore retreats by 1 each bar:

```
Live bar 1: entry bar is at window index 1497
Live bar 2: entry bar is at window index 1496
Live bar 3: entry bar is at window index 1495
…
```

But `state.entry_index` is frozen at 1498 (set when `i = 1498`). It is never
updated. As the window slides, the index drifts further from the true position
of the entry bar — but the subtraction `1498 − 1498 = 0` does not change.

---

## 4. Scope — two affected position classes

### Class A: live-entered positions (primary)

Position that enters during the LIVE LOOP:

- `state.entry_index = 1498` at entry (i = 1498 on that bar)
- Every subsequent bar: `bars_held = 1498 − 1498 = 0`
- **Permanently stuck at 0**

This is the observed NAS100 case.

### Class B: warmup-entered positions (secondary)

Position that enters during `_build_warmed_state` warmup replay:

- Warmup iterates `i` from 0 → 1498. At entry bar E: `state.entry_index = E`
- Warmup final bar: `bars_held_warmup = 1498 − E` (correct)
- First live bar: `i = 1498`, `entry_index = E`, `bars_held = 1498 − E`
  — this equals the warmup-final value, which is numerically correct for bar 0
  of the live window. But on live bar 2, the window has shifted by 1, the entry
  bar is now at window index E−1, while `state.entry_index` is still E.
  `bars_held = 1498 − E` (unchanged). Should be `1498 − E + 1`.
- **bars_held freezes at the warmup-end value. Correct once, then permanently
  stale.**

For strategies where the warmup-end `bars_held >= threshold`, the exit
condition is satisfied immediately on the first live Kalman flip. For
strategies where `bars_held_warmup < threshold`, the condition is never
satisfied in live mode.

---

## 5. Not a backtest bug

`_build_warmed_state` is correct:

```python
for i in range(0, last_closed + 1):   # i = 0, 1, 2, … 1498
    evaluate_bar(df_local, i, state, strategy, config)
```

`i` genuinely increments; `bars_held = i − entry_index` advances correctly.
Backtest behavior is unchanged by this bug. Phase A parity (9/9, 4,035 trades
byte-identical) is unaffected. The bug manifests only in the live sidecar's
rolling-window evaluation path.

---

## 6. Why bars_held = 0 never triggers the exit

`62_TREND_IDX_5M_KALFLIP_S01_V2_P15`, `check_exit` (lines 236–238):

```python
bars_held = ctx.get('bars_held', 0) or 0
if bars_held < self._MIN_HOLD_BARS:    # _MIN_HOLD_BARS = 20
    return False   # ATR stop still active; opposite-signal exit suppressed
```

`0 < 20` is always True → function returns False every bar → no signal exit
ever fires in the sidecar for a live-entered position → shadow position is
permanently stuck → slot remains "in_pos" → `check_entry` is never evaluated
→ no new signals are journaled → presence divergences.

The SL resolver (`resolve_exit`, called unconditionally at lines 524–530) does
not read `bars_held`, so SL-triggered exits still function. This confirms the
observed pattern: only the opposite-signal exit path is broken; the SL path
remains intact.

---

## 7. Divergence pattern confirmed

Both Epoch 5 in-scope divergences follow the same signature:

| field | bar 1 | bar 2 |
|---|---|---|
| `bar_ts` | `2026-05-05 23:10 UTC` | `2026-05-06 04:05 UTC` |
| `detected_utc` | `2026-05-05T20:32:10Z` | `2026-05-06T01:11:48Z` |
| `ts_engine` | `ABSENT` | `ABSENT` |
| `ts_execution` | `PRESENT` | `PRESENT` |
| `category` | `presence` | `presence` |

TS_Execution correctly exits (Kalman flip, bars_held ≥ 20) → slot is flat →
new Kalman flip entry fires → journaled. Sidecar: stuck in Trade 3 (bars_held
= 0 forever) → no exit → slot still "in_pos" → check_entry never evaluated →
not journaled → divergence.

---

## 8. Relationship to earlier ENVIRONMENT_LIFECYCLE_OFFSET classification

The AUDJPY divergence (`2026-05-05 11:30 UTC`) was classified
`ENVIRONMENT_LIFECYCLE_OFFSET` because the sidecar confirms positions 1 bar
later than TS_Execution and was documented as a 1-bar counting offset.

That classification was correct for what could be observed at the time. This
investigation reveals the deeper cause: the rolling-window indexing makes
`bars_held` not merely 1-bar low but **permanently frozen at entry-bar value
(0 for live-entered positions)**. For AUDJPY's `max_bars=2` strategy, the
effect was coincidentally masked at the first detectable bar (bars_held=0 vs
2 looks like a 2-bar offset; the position would never exit via signal either).

The two divergence classes share the same root code path. P1.5 must address
the rolling-window indexing problem — not merely the confirmation-bar offset.

---

## 9. Constraint compliance at investigation stage

- No changes to `monitor.py`, `status_phase_b.py`, `status_phase_b_epoch5.py`
- No changes to `evaluate_bar.py` or any engine code
- No changes to strategy files
- No changes to indicator files
- No process restart during investigation
- `divergence_log.jsonl` untouched

---

*See `PATCH_PLAN.md` for fix design, risk analysis, and validation plan.*
