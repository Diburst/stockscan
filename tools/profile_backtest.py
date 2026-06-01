"""Shim: ``python tools/profile_backtest.py …`` → ``stockscan backtest profile …``.

The actual profiling logic now lives in :mod:`stockscan.backtest.profile`
and is exposed as the ``stockscan backtest profile`` typer subcommand
(DESIGN §4.4.1). This file remains for two reasons:

  1. Ad-hoc invocation without the venv-installed ``stockscan`` script —
     useful when iterating on `profile.py` itself, or when a CI sandbox
     hasn't run ``uv sync`` yet but does have the source on PYTHONPATH.
  2. Muscle memory / historical command lines in scripts.

Every flag passed to this script is forwarded verbatim to the typer
command, so the two invocations are interchangeable:

    python tools/profile_backtest.py reversal_swing --from 2023-01-01
    stockscan backtest profile reversal_swing --from 2023-01-01

Run ``--help`` against either for the full flag list.
"""

from __future__ import annotations

import sys

from stockscan.cli import app


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    app(["backtest", "profile", *args])


if __name__ == "__main__":
    main()
