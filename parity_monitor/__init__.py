"""
TS_Engine parity_monitor — Phase B live divergence detector.

Tails TS_Execution/journal/SignalJournal.jsonl and TS_Engine/journal/
shadow_signal_journal.jsonl. Aligns by (strategy_id, bar_ts). For each
matched pair, field-by-field compare. Any unexplained mismatch is written
to TS_Engine/divergence_log.jsonl.
"""
