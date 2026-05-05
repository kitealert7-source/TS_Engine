# OBSERVABILITY_CANONICAL_HASH_V2
"""
Canonical OHLC SHA-256 hashing for cross-runtime input-window parity.

This file MUST be byte-identical to its sibling at:
    TS_Execution/src/observability_hash.py

Any change requires updating BOTH copies and bumping the version marker
at the top of this file. The marker `OBSERVABILITY_CANONICAL_HASH_V2`
is the discriminator-script's compatibility check.

Hash contract (CANONICAL — frozen by version marker):
  1. Required columns, in this order: ('open', 'high', 'low', 'close')
  2. **Forming bar excluded** (V2): hash is computed over `df.iloc[:-1]`,
     not `df.iloc[:]`. The last row of every poll-derived DataFrame is the
     in-progress bar whose OHLC ticks change continuously between polls;
     including it produced inter-runtime hash drift even when every closed
     bar was byte-identical (V1 deficiency, observed 2026-05-05).
  3. dtype: numpy float64 (forced cast eliminates int/float drift)
  4. layout: C-contiguous, row-major (NumPy default; ascontiguousarray applied
     defensively so a transposed view doesn't change the hash)
  5. bytes: arr.tobytes()
  6. digest: hashlib.sha256(...).hexdigest()  (lowercase, 64 hex chars)

Volume is INTENTIONALLY EXCLUDED so live MT5 (`tick_volume`) and research
CSVs (`volume`) produce comparable hashes across data sources.

This module performs no I/O, no logging, no logic. Pure function.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

__all__ = [
    "CANONICAL_HASH_VERSION",
    "CANONICAL_HASH_COLUMNS",
    "ohlc_sha256",
]

CANONICAL_HASH_VERSION = "OBSERVABILITY_CANONICAL_HASH_V2"
CANONICAL_HASH_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")


def ohlc_sha256(df: pd.DataFrame) -> str:
    """Return cross-runtime canonical SHA-256 of OHLC values in `df`.

    V2: the last row (forming bar, by convention `df.iloc[-1]`) is excluded.
    The strategy / engine evaluates `df.iloc[-2]` (last closed bar); the
    forming bar is never read for any decision. Excluding it from the hash
    eliminates inter-poll tick drift as a source of false hash mismatches.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain columns 'open', 'high', 'low', 'close' and at least
        two rows (one closed + one forming). Any extra columns are ignored.
        Index is not part of the hash.

    Returns
    -------
    str
        Lowercase 64-character hex digest.

    Raises
    ------
    KeyError
        If any of the required OHLC columns is missing.
    ValueError
        If the DataFrame has fewer than 2 rows (no closed bar to hash).
    """
    for c in CANONICAL_HASH_COLUMNS:
        if c not in df.columns:
            raise KeyError(
                f"ohlc_sha256: missing required column '{c}' "
                f"(have: {list(df.columns)})"
            )
    if len(df) < 2:
        raise ValueError(
            f"ohlc_sha256: df has {len(df)} rows; need at least 2 "
            f"(one closed + one forming) so iloc[:-1] is non-empty"
        )
    arr = df.loc[:, list(CANONICAL_HASH_COLUMNS)].iloc[:-1].astype(np.float64).values
    if not arr.flags["C_CONTIGUOUS"]:
        arr = np.ascontiguousarray(arr)
    return hashlib.sha256(arr.tobytes()).hexdigest()
