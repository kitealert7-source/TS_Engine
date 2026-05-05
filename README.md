# TS_Engine

New architecture root for the engine-extraction migration.

This folder is created as part of the **48h Architecture Spike** that proves
the v1.5.8 research engine's per-bar block can be lifted into a standalone
callable (`evaluate_bar()`) reusable by both research and live runtimes.

## Status

**Spike phase only.** No production code yet. Folder structure:

```
TS_Engine/
├── README.md                        — this file
├── tests/
│   └── spike_parity.py              — v1.5.8 vs v1.5.9 byte-identical comparison
└── spike_artifacts/                 — outputs of parity runs (signals/trades/ledger)
```

The full live runtime (`live/`, `data/`, etc.) is built only AFTER the spike
authorizes Phase A/B/C.

## Spike acceptance criterion

For each of {33_TREND_BTCUSD, 62_TREND_IDX_5M_KALFLIP, 27_MR_XAUUSD_PINBAR},
v1.5.8 and v1.5.9 must produce byte-identical trade lists when run on the
same input bars. Zero tolerance.

## After spike

If parity passes: Phase A (full extraction) → Phase B (live shadow sidecar) → Phase C (cutover).

If any strategy fails: spike halts, classify, recommend B (minor refactor) or C (architecture not extraction-ready).
