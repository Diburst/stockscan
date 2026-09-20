"""Options-premium proposal engine.

A cross-sectional layer that turns per-symbol ``options_context`` into a ranked,
regime-sized, diversified book of short-premium trade proposals. It is NOT a
``Strategy`` (those are per-symbol/directional); it's a sibling to the scan
runner that consumes ``SymbolAnalysis`` and constructs a portfolio.

Reads like a book: the trigger, side selection, hard filters and rank key live
inline in ``engine.py``; the book multiplier, contract sizing and the
diversification caps in ``portfolio.py``. DESIGN.md §4.16 is the design note.
"""

from __future__ import annotations

from stockscan.proposals._models import OptionProposal
from stockscan.proposals.engine import propose_candidates
from stockscan.proposals.portfolio import build_book
from stockscan.proposals.service import generate_book

__all__ = ["OptionProposal", "build_book", "generate_book", "propose_candidates"]
