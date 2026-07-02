"""Delta hedging — real-time stock hedging of a short/long option position.

Layers (each in its own module, "reads like a book"):
  * :mod:`hedge.policy`      — HedgePolicy: the no-transaction band (Whalley-Wilmott
                               default) that decides *when* to rehedge.
  * :mod:`hedge.accounting`  — pure P&L + settlement math (no I/O).
  * :mod:`hedge.feed`        — real-time price feeds (EODHD websocket + simulated).
  * :mod:`hedge.vol`         — realized-vol σ for the delta calc.
  * :mod:`hedge.store`       — Postgres CRUD + adjustment ledger + heartbeat.
  * :mod:`hedge.service`     — settle-and-close + live P&L, shared by daemon + web.
  * :mod:`hedge.daemon`      — the asyncio process that ties it together.

The delta math itself lives in :mod:`stockscan.analysis.black_scholes`
(``position_delta`` / ``position_gamma`` / ``hedge_target_shares``).
"""

from __future__ import annotations

from stockscan.hedge.policy import (
    FIXED_SHARES,
    PCT_MOVE,
    WHALLEY_WILMOTT,
    HedgePolicy,
)

__all__ = [
    "FIXED_SHARES",
    "PCT_MOVE",
    "WHALLEY_WILMOTT",
    "HedgePolicy",
]
