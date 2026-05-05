# Phase B — Live Shadow Sidecar Plan

**Status:** Components built; awaiting user start of 5-day soak run.
**Authorization:** Phase A passed (9/9 strategies byte-identical, 4,035 trades).
**Rule:** TS_Engine is observer only. Zero dispatch authority. TS_Execution remains authoritative.

---

## Architecture

```
                    ┌──────────────────┐
                    │   MT5 Terminal   │
                    └──────────────────┘
                       ▲          ▲
              order/   │          │  read-only
              read     │          │  (copy_rates_from_pos)
                       │          │
       ┌───────────────┴──┐    ┌──┴──────────────────┐
       │  TS_Execution    │    │  TS_Engine          │
       │  (authoritative) │    │  (observer only)    │
       │                  │    │                     │
       │  bar_loop        │    │  live_runtime/      │
       │    ↓             │    │    runner.py        │
       │  pipeline.py     │    │    bar_loop.py      │
       │    ↓             │    │    (calls v1.5.9    │
       │  dispatch        │    │     evaluate_bar)   │
       │    ↓             │    │    ↓                │
       │  SignalJournal   │    │  shadow_signal_     │
       │    .jsonl        │    │    journal.jsonl    │
       └──────────────────┘    └─────────────────────┘
                ▲                       ▲
                │                       │
                └──────────┬────────────┘
                           │
                  ┌────────┴─────────┐
                  │ parity_monitor/  │
                  │ tails both       │
                  │ journals;        │
                  │ field-by-field   │
                  │ comparison       │
                  │    ↓             │
                  │ divergence_      │
                  │   log.jsonl      │
                  └──────────────────┘
```

### Why two MT5 read-only connections is safe

MT5 supports concurrent terminals/accounts and concurrent read calls within a single terminal. TS_Execution holds ONE order-capable connection; TS_Engine holds a SECOND read-only connection that calls only `copy_rates_from_pos` and `symbol_info`. There is no order_send, no positions_get-for-write, no trade_request. **No racing for order execution.**

Bar-fetch races are also safe: both connections see the same bar data once the broker has published it. TS_Engine intentionally polls **20 seconds after** the expected bar close to let TS_Execution detect first — eliminating cache-staleness ambiguity.

---

## What's built (this commit)

```
TS_Engine/
├── PHASE_B_PLAN.md                      ← this file
├── divergence_log.jsonl                 ← runtime output (created on first divergence)
├── live_runtime/
│   ├── __init__.py
│   ├── runner.py                        ← daemon entry point
│   ├── bar_loop.py                      ← per-(symbol, tf) bar callback
│   ├── mt5_reader.py                    ← read-only MT5 wrapper
│   └── shadow_journal.py                ← writes shadow_signal_journal.jsonl
├── parity_monitor/
│   ├── __init__.py
│   ├── monitor.py                       ← daemon: tail + compare
│   └── compare.py                       ← field-by-field diff
└── journal/
    └── shadow_signal_journal.jsonl      ← TS_Engine's signal output (created at first signal)
```

---

## Phase B success gate

ALL conditions required before Phase C authorization:

1. **5 trading days** — Mon-Fri elapsed continuously without crash
2. **≥30 non-null signals** — both TS_Execution and TS_Engine emitted at least 30 signals in aggregate
3. **0 unexplained divergences** — `divergence_log.jsonl` is empty OR every entry is annotated with a non-architectural cause (e.g. clock skew on bar timestamp)

**On any divergence:** stop, classify, write `PHASE_B_REPORT.md`. No patching during run.

Categories of acceptable explained divergences (none expected):
- (a) **timestamp formatting** — TS_Execution writes "2026-05-01 03:15 UTC", TS_Engine could write "2026-05-01T03:15:00Z". Same instant, different format. **Comparator normalizes both to ISO8601 UTC** before comparison so this won't trigger.
- (b) **regime cache version skew** — if TS_Execution updated regime cache between TS_Engine's poll and TS_Execution's poll, they could see different regime IDs. **Mitigated by 20s lag — TS_Execution always writes first.**

Any other divergence is unexplained and triggers stop + classify + report.

---

## How to run

### Start TS_Engine sidecar (in a separate terminal from TS_Execution)

```bash
# From TS_Engine root:
python live_runtime/runner.py
```

This starts:
- One thread per (symbol, timeframe) group present in TS_Execution's portfolio.yaml
- Read-only MT5 connection to OctaFx-Real
- Polls bars 20 seconds after each expected bar close
- Writes signals to `journal/shadow_signal_journal.jsonl`

### Start parity monitor (in another terminal)

```bash
python parity_monitor/monitor.py
```

This:
- Tails both `TS_Execution/journal/SignalJournal.jsonl` and `TS_Engine/journal/shadow_signal_journal.jsonl`
- Aligns entries by (strategy_id, bar_ts)
- Field-by-field comparison
- Writes any divergences to `divergence_log.jsonl`

### Check progress against the gate

```bash
python tools/status_phase_b.py
```

Prints:
- Days running
- Total signals from each side
- Total aligned pairs
- Total divergences
- Gate status: PASS / FAIL / IN_PROGRESS

### Stop everything cleanly

Ctrl-C in each terminal. The sidecar will finalize its open file handles and exit. **No state in TS_Execution is affected.**

---

## What this build does NOT do

Per the rules, the following are **explicitly disabled** in TS_Engine's runtime:
- ❌ No order placement (no `mt5.order_send`)
- ❌ No bridge file writes (no IPC)
- ❌ No position queries (no `positions_get`)
- ❌ No account writes (no `account_info_set`)
- ❌ No watchdog interaction
- ❌ No Task Scheduler entries
- ❌ No modifications to `TS_Execution/`
- ❌ No modifications to v1.5.8 engine

If any of the above happen, it's a bug. The runtime will hard-fail rather than silently call a forbidden API.

---

## Code review hooks

Reviewers should grep TS_Engine for these strings to verify safety invariants:

```bash
grep -rn "order_send\|positions_get\|trade_request" TS_Engine/      # should be ZERO matches
grep -rn "open.*bridge\|bridge.*write" TS_Engine/                   # should be ZERO matches
grep -rn "TS_Execution" TS_Engine/                                  # only allowed: read-only journal paths
```

---

## After 5-day soak passes

If gate met (5 days, ≥30 signals, 0 unexplained divergences):
- Write `PHASE_B_REPORT.md` summarizing the run
- Authorize Phase C (cutover with kill switch)

If gate fails:
- `PHASE_B_REPORT.md` classifies the failure
- No Phase C authorization
- Decision point on next steps (depends on classification)

**No live cutover, no merge to main, no broker-state writes during Phase B.**
