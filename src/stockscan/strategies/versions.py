"""Strategy-version SQL helpers.

When a strategy's class version is bumped (Donchian 1.0.0 → 1.1.0,
say), historical signals from the older version stay in the database
intentionally — they're useful for offline comparison and audit. But
the live web UI and the meta-label trainer should default to ONLY the
current registered version, so the user isn't comparing apples to
oranges across signal-generation eras.

This module exposes one helper, :func:`current_version_filter`, that
returns a SQL ``WHERE``-clause fragment + parameter dict for filtering
``signals`` / ``strategy_runs`` queries to the registered current
version of EACH strategy. The clause is composed of ``OR``-ed
``(strategy_name, strategy_version)`` pairs read from
:data:`STRATEGY_REGISTRY`, so it stays correct as strategies are added
or version-bumped — no separate config to maintain.

CLI commands that legitimately want to look at older versions
(``stockscan ml train --version 1.0.0``, ``stockscan signals delete
--version 1.0.0``) bypass this helper and filter on the explicit
version directly.
"""

from __future__ import annotations

from stockscan.strategies.base import STRATEGY_REGISTRY


def current_version_filter(
    *, prefix: str = "s",
) -> tuple[str, dict[str, str]]:
    """Build a SQL ``WHERE``-fragment restricting rows to the current
    registered version of each strategy.

    Parameters
    ----------
    prefix:
        Column prefix on the table being filtered, e.g. ``"s"`` for a
        ``signals s`` alias, or ``"r"`` for a ``strategy_runs r``
        alias. The fragment uses ``{prefix}.strategy_name`` and
        ``{prefix}.strategy_version`` so it composes with arbitrary
        joins.

    Returns
    -------
    tuple[str, dict[str, str]]
        ``(clause, params)`` where:
          * ``clause`` is a parenthesised ``OR`` of per-strategy
            ``(name = X AND version = Y)`` pairs. Caller prepends
            ``AND`` if combining with other filters.
          * ``params`` maps the bind names embedded in the clause to
            their string values.

        If no strategies are registered, returns ``("1=0", {})`` so
        any query using the result emits no rows rather than crashing.

    Example
    -------
    >>> clause, params = current_version_filter(prefix="s")
    >>> clause
    '((s.strategy_name = :svn0 AND s.strategy_version = :svv0)
       OR (s.strategy_name = :svn1 AND s.strategy_version = :svv1))'
    >>> params
    {'svn0': 'donchian_trend', 'svv0': '1.1.0',
     'svn1': 'rsi2_meanrev', 'svv1': '1.0.0'}
    """
    strategies = STRATEGY_REGISTRY.all()
    if not strategies:
        return "1=0", {}

    pairs: list[str] = []
    params: dict[str, str] = {}
    for i, cls in enumerate(strategies):
        n_key = f"svn{i}"
        v_key = f"svv{i}"
        pairs.append(
            f"({prefix}.strategy_name = :{n_key} "
            f"AND {prefix}.strategy_version = :{v_key})"
        )
        params[n_key] = cls.name
        params[v_key] = cls.version
    return "(" + " OR ".join(pairs) + ")", params
