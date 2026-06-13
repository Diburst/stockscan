-- 0020_watchlist_lists.sql
--
-- Multiple named watchlists.
--
-- Until now the watchlist was a single flat mega-list: one row per symbol
-- in `watchlist_items`. This migration adds a many-to-many layer so a
-- symbol can belong to several named lists at once (e.g. AAPL on both
-- "Tech" and "Earnings plays").
--
-- Design note: `watchlist_items` stays the per-symbol entity and keeps
-- owning the price target, alert state, and note — those are inherently
-- per-symbol, not per-list. The new `watchlists` table holds the named
-- lists, and `watchlist_membership` is the join. The existing
-- UNIQUE(symbol) on watchlist_items is therefore preserved.

CREATE TABLE watchlists (
    list_id     BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE watchlist_membership (
    list_id       BIGINT NOT NULL REFERENCES watchlists (list_id) ON DELETE CASCADE,
    watchlist_id  BIGINT NOT NULL REFERENCES watchlist_items (watchlist_id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (list_id, watchlist_id)
);

-- Fast "which lists is this symbol on" + "what's on this list" lookups.
CREATE INDEX idx_membership_watchlist_id ON watchlist_membership (watchlist_id);

-- Seed the default list and fold every pre-existing symbol into it so the
-- old mega-list is preserved verbatim as a list named "Watchlist".
INSERT INTO watchlists (name) VALUES ('Watchlist');

INSERT INTO watchlist_membership (list_id, watchlist_id)
SELECT (SELECT list_id FROM watchlists WHERE name = 'Watchlist'),
       w.watchlist_id
FROM watchlist_items w;
