# PATCH_PLAN — Persistent Live-Sidecar Position Aging Fix

**Targets:** `ENGINE-HARD-LIVE-MODE` — `bars_in_position` rolling-window freeze  
**Date:** 2026-05-06  
**Constraint set:** No evaluate_bar changes · No strategy changes · No indicator changes ·
No backtest drift · Minimal surgical diff  

---

## 1. Files to modify

| File | Change type |
|---|---|
| `TS_Engine/live_runtime/bar_loop.py` | Primary fix — only file that changes |

**Nothing else changes.** Specifically:

| File | Status |
|---|---|
| `.../v1_5_9/evaluate_bar.py` | **UNTOUCHED** — no behavioral drift |
| `parity_monitor/monitor.py` | **UNTOUCHED** — harness frozen |
| `tools/status_phase_b*.py` | **UNTOUCHED** — harness frozen |
| `engines/`, `engine_dev/` | **UNTOUCHED** — engine frozen |
| Any `strategies/*.py` | **UNTOUCHED** |
| Any `indicators/*.py` | **UNTOUCHED** |
| `TS_Execution/src/*.py` | **UNTOUCHED** — separate runtime |

---

## 2. Design — persistent counter in StrategyState

### 2.1 Core idea

`evaluate_bar` computes `bars_held = i - state.entry_index`. In the live loop,
`i = 1498` always. We cannot change `evaluate_bar` (frozen). Instead, we
maintain a persistent counter `bars_in_position_live` in `StrategyState` and
synthesize a correct `state.entry_index` before each live call:

```
state.entry_index = latest_closed_idx - bars_in_position_live
               ↓ inside evaluate_bar
bars_held = i - state.entry_index
          = 1498 - (1498 - bars_in_position_live)
          = bars_in_position_live   ✓
```

The counter increments once per live bar while in position, is reset on
entry/exit, and is initialized from the warmup replay state on startup.

`evaluate_bar.py` is never modified. The backtest path (`_build_warmed_state`
iterates `i = 0…1498` with correct advancing index) is never altered.

### 2.2 Counter semantics

| Event | `bars_in_position_live` value |
|---|---|
| Not in position | 0 |
| Pending entry set (signal bar) | 0 (not yet in position) |
| Entry execution bar (fills at open) | 0 (set after evaluate_bar returns) |
| First bar after entry | 1 |
| Second bar after entry | 2 |
| … | … |
| Exit bar | N (incremented before evaluate_bar; `bars_held = N` in trade dict) |
| After exit | reset to 0 |

This matches `evaluate_bar`'s backtest semantics exactly: `bars_held = 0` on
entry execution bar (because evaluate_bar sets entry_index=i and returns
immediately), `bars_held = 1` on the first evaluation bar, etc.

### 2.3 Initialization from warmup

After `_build_warmed_state` completes, if a position is open, the counter
is initialized from the warmup's final computed value:

```python
bars_in_position_live_init = (last_idx - state.entry_index) if state.in_pos else 0
```

`last_idx = 1498` (last warmup bar), `state.entry_index = E` (entry bar in
warmup). Result: `1498 - E` — the same value `evaluate_bar` computed at the
final warmup bar. The live counter then continues advancing from this value on
the first live bar. No restart discontinuity.

---

## 3. Exact diff — `bar_loop.py`

### Change A: `StrategyState` dataclass (line ~80)

**Current:**
```python
@dataclass
class StrategyState:
    """Per-strategy mutable state held across bar evaluations."""
    strategy_id: str
    strategy:    Any
    bar_state:   BarState
    config:      EngineConfig
    last_pending_bar_idx: int = -1
```

**After:**
```python
@dataclass
class StrategyState:
    """Per-strategy mutable state held across bar evaluations."""
    strategy_id: str
    strategy:    Any
    bar_state:   BarState
    config:      EngineConfig
    last_pending_bar_idx: int = -1
    bars_in_position_live: int = 0   # persistent counter; see PATCH_PLAN.md §2
```

---

### Change B: warmup-state initialization (line ~238, inside the startup loop)

**Current:**
```python
states.append(StrategyState(
    strategy_id=strategy.name,
    strategy=strategy,
    bar_state=state,
    config=config,
    last_pending_bar_idx=last_idx,
))
```

**After:**
```python
_bipl_init = (last_idx - state.entry_index) if state.in_pos else 0
states.append(StrategyState(
    strategy_id=strategy.name,
    strategy=strategy,
    bar_state=state,
    config=config,
    last_pending_bar_idx=last_idx,
    bars_in_position_live=_bipl_init,
))
```

---

### Change C: live loop — counter maintenance + entry_index override
(before line 322, inside the per-strategy `for ss in states:` block)

**Current** (lines 321–329):
```python
_bs = ss.bar_state
_in_pos_before        = bool(_bs.in_pos)
_direction_before     = int(_bs.direction)
_entry_index_before   = int(_bs.entry_index)
_stop_price_before    = (float(_bs.stop_price_active)
                         if _bs.stop_price_active is not None else None)

evaluate_bar(df_eval, latest_closed_idx, ss.bar_state,
             ss.strategy, ss.config)

# OBSERVABILITY_CANONICAL_HASH_V1 — decision post-snapshot
_in_pos_after    = bool(ss.bar_state.in_pos)
_direction_after = int(ss.bar_state.direction)
```

**After:**
```python
_bs = ss.bar_state
_in_pos_before        = bool(_bs.in_pos)
_direction_before     = int(_bs.direction)
_stop_price_before    = (float(_bs.stop_price_active)
                         if _bs.stop_price_active is not None else None)

# POSITION-AGING FIX: synthesize entry_index for rolling-window live mode.
# evaluate_bar computes bars_held = (i - state.entry_index).  In live mode
# i = latest_closed_idx = 1498 forever, so bars_held would freeze at 0 for
# any position entered during the live loop. We maintain a persistent counter
# and back-compute the entry_index that yields the correct bars_held.
# evaluate_bar.py is not modified; backtest path is not affected.
# See ROOT_CAUSE_CONFIRMATION.md + PATCH_PLAN.md for full analysis.
if _in_pos_before:
    ss.bars_in_position_live += 1
    _bs.entry_index = latest_closed_idx - ss.bars_in_position_live

_entry_index_before = int(_bs.entry_index)   # capture after override

evaluate_bar(df_eval, latest_closed_idx, ss.bar_state,
             ss.strategy, ss.config)

# OBSERVABILITY_CANONICAL_HASH_V1 — decision post-snapshot
_in_pos_after    = bool(ss.bar_state.in_pos)
_direction_after = int(ss.bar_state.direction)

# POSITION-AGING FIX: update counter on transitions
if not _in_pos_before and _in_pos_after:
    # Pending entry just executed this bar (entry_index set to 1498 by
    # evaluate_bar). Counter starts at 0; increments on the NEXT bar.
    ss.bars_in_position_live = 0
if _in_pos_before and not _in_pos_after:
    # Exit fired this bar.  Reset counter.
    ss.bars_in_position_live = 0
```

**Note on `_entry_index_before` capture order:** The capture is moved to
AFTER the entry_index override. This means `_entry_index_before` now equals
`latest_closed_idx - bars_in_position_live` (the synthetic correct value).
All downstream uses of `_entry_index_before` benefit:

- BAR_TELEMETRY `bars_in_position = latest_closed_idx - _entry_index_before`
  → equals `bars_in_position_live` ✓
- EXIT_JOURNAL `entry_bar_ts = df_eval.index[_entry_index_before]`
  → `df_eval.index[1498 - N]` = entry bar N bars back in current window ✓
- EXIT_JOURNAL `bars_in_position = latest_closed_idx - _entry_index_before`
  → equals `bars_in_position_live` ✓
- BAR_TELEMETRY `entry_index_before` field value changes from 1498 (broken) to
  a synthetic offset (1498 − N). **This is a telemetry field change.** See §4.3.

---

### Change D: no other bar_loop.py changes required

The BAR_TELEMETRY write block (lines 336–382) and EXIT_JOURNAL write block
(lines 385–420) both use `_entry_index_before` and the expression
`latest_closed_idx - _entry_index_before`. After Change C, these expressions
compute correctly without further modification.

The signal-journaling block (lines 424–468) does not use `entry_index` at all
and requires no change.

---

## 4. Risk analysis

### 4.1 Backtest parity — NO RISK

`evaluate_bar.py` is untouched. The warmup replay (`_build_warmed_state`)
calls `evaluate_bar(df_local, i, state, ...)` with `i` advancing 0→1498. The
override in Change C is inside the LIVE LOOP (`while not stop_flag.is_set()`),
not in `_build_warmed_state`. The entry_index is not overridden during warmup.

Phase A parity test (9/9 strategies, 4,035 trades, byte-identical) remains
valid and verifiable. No re-run of Phase A is required by this change.

### 4.2 TS_Execution — NO IMPACT

TS_Execution is a separate process. It has no code dependency on `bar_loop.py`
or `evaluate_bar.py`. It maintains its own `bars_held` counter in
`strategy_slot.py` from which it has never had this bug.

### 4.3 Telemetry field changes — NARROW, DOCUMENTED

**`bar_telemetry.jsonl` — two fields change semantics:**

| Field | Before fix | After fix |
|---|---|---|
| `entry_index_before` | Always 1498 for live-entered positions (broken) | `1498 − N` where N = bars in position (synthetic but meaningful: N bars back in window = entry bar) |
| `bars_in_position` | Always 0 for live-entered positions (broken) | Correctly advances 1, 2, 3… (fixed) |

The `bars_in_position` fix is the goal. The `entry_index_before` change is a
side-effect of using the same override for telemetry and engine logic — the
value changes from a meaningless frozen integer to a meaningful rolling-window
offset.

Downstream consumers of `bar_telemetry.jsonl`:
- `parity_monitor/monitor.py` — does NOT read `bar_telemetry.jsonl`. Reads
  `shadow_signal_journal.jsonl` and `SignalJournal.jsonl` only. No impact.
- `status_phase_b_epoch5.py` — does NOT read `bar_telemetry.jsonl`. No impact.
- Human operator inspection — the corrected values are more useful, not less.

**`exit_signal_journal.jsonl` — two fields change:**

| Field | Before fix | After fix |
|---|---|---|
| `entry_index` | Always 1498 (broken) | `1498 − N` = window offset to entry bar |
| `bars_in_position` | `0` (broken) | Correctly N (fixed) |
| `entry_bar_ts` | `df_eval.index[1498]` = current bar (wrong) | `df_eval.index[1498 − N]` = actual entry bar timestamp ✓ |

The `entry_bar_ts` fix is significant: before the fix, this field always shows
the current bar's timestamp (because `entry_index_before = 1498 = latest_closed_idx`).
After the fix, it correctly shows the bar at which the position opened.

**`shadow_signal_journal.jsonl` / `SignalJournal.jsonl` — NO CHANGE**

Signal journals record signal-bar data, not position-age data. Untouched.

### 4.4 Trade dict in BarState — NO OBSERVABLE IMPACT

When `evaluate_bar` fires an exit, it builds a trade dict (lines 624–686)
with `"bars_held": i - state.entry_index`. After the fix, `state.entry_index`
has been overridden to `1498 - N`, so `bars_held = 1498 - (1498 - N) = N`
(correct). The trade dict field is correct. In live sidecar mode, evaluate_bar's
return value is not directly logged to any journal (bar_loop.py does not
capture the `evaluate_bar` return value — line 328–329). No journal is affected.

### 4.5 Partial exit guard (evaluate_bar line 534) — FIXED AS SIDE EFFECT

```python
bars_held_now = i - state.entry_index
```

After the fix, this also computes correctly. No strategy in the current
burn-in portfolio uses `check_partial_exit`, so this is a latent fix with no
immediate observable effect. Risk: zero.

### 4.6 Position age on sidecar restart — CORRECT

On any restart, `_build_warmed_state` replays historical bars with advancing
`i`. If a position is open at warmup end, `state.entry_index = E` (correct
historical bar). Change B initializes `bars_in_position_live = 1498 - E`.
The live counter then continues from that value on the first live bar.
No discontinuity or jump.

### 4.7 Positions open > 1498 bars — OUT OF SCOPE

If a position is held for more than 1498 bars, `latest_closed_idx - N` would
go negative, causing an invalid `entry_index`. For 5m timeframe: 1498 bars =
~5.2 days. No current burn-in strategy is designed to hold that long (Kalman
flip strategy exits on opposite signal typically within hours–days). This edge
case is documented but not guarded — it is outside the operating envelope of
any active strategy.

---

## 5. Migration and restart procedure

### Pre-restart (read-only — do before any code change)

1. Run `python tools/status_phase_b_epoch5.py`. Record output.
2. Record `shadow_trades.jsonl` line count and last entry.
3. Record `bar_telemetry.jsonl` last entry.
4. Confirm soak processes alive:
   ```powershell
   Get-Item outputs/logs/heartbeat.log | Select LastWriteTime
   Get-Content outputs/logs/execution.pid, outputs/logs/watchdog.pid
   ```

### Code change

5. Apply Changes A–C to `bar_loop.py` (only). Verify no other files touched.
6. Commit to `TS_Engine` with message referencing `PATCH_PLAN.md`.

### Restart

7. ```powershell
   cd C:\Users\faraw\Documents\TS_Engine
   python live_runtime/runner.py   # or however the sidecar is invoked
   ```
   The sidecar restart does NOT require restarting TS_Execution or watchdog.
   The sidecar is observer-only; it has no write path to MT5.

### Post-restart verification (first 3 bars)

8. Watch `journal/bar_telemetry.jsonl`. For the NAS100 strategy:
   - `bars_in_position` should be `> 0` on the first bar after restart
     (initialized from warmup value)
   - Should increment by 1 each bar
9. Confirm no new divergences appear in `divergence_log.jsonl`.

---

## 6. Validation plan

### 6.1 Replay proof (offline, no restart required)

Run `_build_warmed_state` in isolation with the NAS100 strategy over a window
that includes the May 5 entry. Verify:

```python
state, last_idx = _build_warmed_state(df_1500, strategy, config)
assert state.in_pos == True
assert state.entry_index < last_idx          # entry is before warmup end
bars_held_warmup = last_idx - state.entry_index
assert bars_held_warmup > 0                  # position has age
```

Then simulate the live loop for 25 bars using the fixed counter logic:

```python
counter = bars_held_warmup
for live_bar_n in range(1, 26):
    counter += 1
    synthetic_entry_index = 1498 - counter
    simulated_bars_held = 1498 - synthetic_entry_index  # = counter
    assert simulated_bars_held == bars_held_warmup + live_bar_n
```

Verify that after counter reaches 20, a mock `check_exit` with a Kalman flip
would return True.

### 6.2 Live proof (post-restart, in-soak)

- `bar_telemetry.jsonl` for `62_TREND_IDX_5M_KALFLIP_S01_V2_P15`:
  - `bars_in_position` field must advance by 1 each bar
  - `entry_bar_ts` in `exit_signal_journal.jsonl` must match the actual entry
    bar timestamp (not the current bar)
- `shadow_signal_journal.jsonl`: no new presence divergences from this strategy
  after fix is live

### 6.3 No-regression proof

**Backtest regression:**
```bash
cd TS_Engine
python tools/smoke_dispatch.py --strategy 62_TREND_IDX_5M_KALFLIP_S01_V2_P15
```
Expect no new divergences. Output should be identical to pre-fix run (smoke
dispatch uses `evaluate_bar` directly via `_build_warmed_state`; the live-loop
override path is not exercised).

**Gate regression:**
```bash
python tools/status_phase_b_epoch5.py
```
After excluding the two NAS100 divergences (see §7), gate should show:
`In Epoch 5 scope: 0  OK`.

**Signal integrity:**
Verify `shadow_signal_journal.jsonl` continues to emit correct signals for the
NAS100 strategy after fix: entry signals should appear shortly after warmup,
and exit signals should appear when Kalman flips at bars_held ≥ 20.

### 6.4 Acceptance criteria

| Check | Pass condition |
|---|---|
| `bar_telemetry.jsonl` `bars_in_position` | Advances 1 per bar; > 0 within 3 bars of restart |
| `exit_signal_journal.jsonl` `entry_bar_ts` | Matches known entry bar timestamp |
| `exit_signal_journal.jsonl` `bars_in_position` | ≥ 20 on NAS100 signal exits |
| No new KALFLIP presence divergences post-fix | `divergence_log.jsonl` clean |
| Gate status | `IN_PROGRESS` (no FAIL) after exclusion of 2 pre-fix records |
| Backtest smoke | Zero divergences (byte-identical output) |

---

## 7. Post-fix divergence exclusion

After the fix is validated live, exclude the two pre-fix NAS100 divergences:

```json
{
    "bar_ts": "2026-05-05 23:10 UTC",
    "strategy_id": "62_TREND_IDX_5M_KALFLIP_S01_V2_P15",
    "detected_utc": "2026-05-05T20:32:10+00:00",
    "excluded_reason": "ENVIRONMENT_LIFECYCLE_OFFSET",
    "excluded_utc": "<timestamp at exclusion>",
    "root_cause": "bars_in_position rolling-window freeze: evaluate_bar computed bars_held = i − entry_index = 1498 − 1498 = 0 always for live-entered positions. Strategy _MIN_HOLD_BARS=20 gate permanently locked. Fixed in bar_loop.py PATCH_PLAN.md 2026-05-06.",
    "post_cutover_fix": "Resolved by PATCH_PLAN.md deployment — bar_loop.py persistent counter fix."
},
{
    "bar_ts": "2026-05-06 04:05 UTC",
    "strategy_id": "62_TREND_IDX_5M_KALFLIP_S01_V2_P15",
    "detected_utc": "2026-05-06T01:11:48+00:00",
    "excluded_reason": "ENVIRONMENT_LIFECYCLE_OFFSET",
    "excluded_utc": "<timestamp at exclusion>",
    "root_cause": "Same as 2026-05-05 23:10 UTC. Pre-fix divergence.",
    "post_cutover_fix": "Resolved by PATCH_PLAN.md deployment — bar_loop.py persistent counter fix."
}
```

Append to `TS_Engine/divergence_exclusions.json`. Gate returns to clean.

**Note:** The AUDJPY divergence (`2026-05-05 11:30 UTC`) was the same bug class
(bars_held frozen at 0 → max_bars=2 never satisfied). Its exclusion record
already exists and is correct. After the fix, this class of divergence should
not recur.

---

## 8. Updated P1.5 backlog scope

The prior P1.5 description was:
> *Shadow position age alignment: make TS_Engine start bars_in_position from
> signal generation bar, not confirmation bar.*

This was incomplete. The actual fix is:

> **P1.5 — Persistent live-sidecar position age counter**
>
> Root cause: `evaluate_bar` computes `bars_held = i − entry_index` where
> `i = latest_closed_idx = 1498` always in the live rolling-window loop. For
> live-entered positions, `entry_index = 1498` at entry, so `bars_held = 0`
> forever. For warmup-entered positions, `bars_held` freezes at the warmup-end
> value and never advances.
>
> Fix: maintain `bars_in_position_live` counter in `StrategyState`. Increment
> each live bar while in position. Before `evaluate_bar`, synthesize
> `state.entry_index = latest_closed_idx − bars_in_position_live` so that the
> formula inside `evaluate_bar` yields the correct `bars_held`.
>
> Scope: `TS_Engine/live_runtime/bar_loop.py` only. evaluate_bar.py untouched.
> Phase A parity preserved. TS_Execution unaffected.
>
> Status after PATCH_PLAN.md implementation: **RESOLVED**.

---

*See `ROOT_CAUSE_CONFIRMATION.md` for the full three-location code trace.*
