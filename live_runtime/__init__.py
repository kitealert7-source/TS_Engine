"""
TS_Engine live_runtime — Phase B observer-only sidecar.

Hard rule: zero dispatch authority, zero broker writes, zero state mutation
in TS_Execution. This package only READS from MT5 and WRITES to its own
shadow journal.
"""
