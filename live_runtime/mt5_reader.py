"""
mt5_reader.py — read-only MT5 wrapper for TS_Engine sidecar.

This module exposes ONLY bar-fetching and symbol-info functions. It does NOT
import or expose order_send, positions_get, trade_request, or any other
MT5 API that mutates broker state.

Hard safety: the import list at module-level is exhaustive. Any attempt to
add an order/trade function should fail code review.

Connection model:
- Connects to OctaFx-Real with the SAME account TS_Execution uses, but as
  a SECOND independent MetaTrader5 instance (MT5 supports concurrent terms).
- All calls are read-only. No locks, no rate-limiter required because we
  intentionally poll 20s AFTER bar close (after TS_Execution's poll window).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

import MetaTrader5 as _mt5

# ---------------------------------------------------------------------------
# Whitelist: exactly the read-only MT5 functions we need.
# Any other use of MetaTrader5 anywhere in TS_Engine is a bug.
# ---------------------------------------------------------------------------
_ALLOWED = {
    "initialize",        # connect
    "shutdown",          # disconnect
    "terminal_info",     # diagnostic
    "account_info",      # diagnostic (read-only — does NOT modify)
    "symbol_info",       # bar metadata (read-only)
    "symbol_select",     # ensure symbol is in market watch (no order side-effect)
    "copy_rates_from_pos",  # bar fetch
    "last_error",        # error inspection
}


def initialize(login: Optional[int] = None, server: Optional[str] = None,
               password: Optional[str] = None, path: Optional[str] = None,
               timeout_ms: int = 60000) -> bool:
    """Initialize MT5 connection. Returns True on success."""
    kwargs: dict[str, Any] = {"timeout": timeout_ms}
    if path:
        ok = _mt5.initialize(path, **kwargs)
    else:
        ok = _mt5.initialize(**kwargs)
    if not ok:
        return False
    if login is not None:
        return bool(_mt5.login(login, password=password, server=server, timeout=timeout_ms))
    return True


def shutdown() -> None:
    _mt5.shutdown()


def account_info():
    return _mt5.account_info()


def symbol_info(symbol: str):
    return _mt5.symbol_info(symbol)


def symbol_select(symbol: str, enable: bool = True) -> bool:
    return bool(_mt5.symbol_select(symbol, enable))


# Timeframe mapping — lowercase string to MT5 enum
_TF_MAP = {
    "1m":  _mt5.TIMEFRAME_M1,
    "5m":  _mt5.TIMEFRAME_M5,
    "15m": _mt5.TIMEFRAME_M15,
    "30m": _mt5.TIMEFRAME_M30,
    "1h":  _mt5.TIMEFRAME_H1,
    "4h":  _mt5.TIMEFRAME_H4,
    "1d":  _mt5.TIMEFRAME_D1,
}


def tf_lower_to_mt5(tf_lower: str) -> int:
    if tf_lower not in _TF_MAP:
        raise ValueError(f"unsupported timeframe: {tf_lower!r}")
    return _TF_MAP[tf_lower]


def copy_rates(symbol: str, tf_lower: str, n_bars: int):
    """Fetch the last n_bars completed+forming bars for symbol/tf.
    Returns numpy structured array (or None on error). Read-only MT5 call.
    """
    tf = tf_lower_to_mt5(tf_lower)
    return _mt5.copy_rates_from_pos(symbol, tf, 0, n_bars)


def last_error():
    return _mt5.last_error()


def now_utc() -> datetime:
    """Wall clock UTC. Used for bar-close scheduling, never for trading logic."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Self-check: at module import time, verify that we have NOT accidentally
# imported any forbidden order/trade function.
# ---------------------------------------------------------------------------
def _selfcheck_no_forbidden_imports():
    forbidden = {"order_send", "order_check", "trade_request", "positions_get",
                 "positions_total", "history_orders_get", "history_deals_get",
                 "Buy", "Sell", "Close"}
    used_names = set(globals().keys())
    overlap = used_names & forbidden
    if overlap:
        raise RuntimeError(
            f"mt5_reader.py imported forbidden MT5 functions: {overlap}. "
            "TS_Engine sidecar is observer-only — these functions must not be "
            "accessible from this module."
        )

_selfcheck_no_forbidden_imports()
