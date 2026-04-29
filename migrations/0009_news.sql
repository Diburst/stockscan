-- 0009_news.sql
--
-- Financial news integration (DESIGN §news).
--
-- Four tables:
--   news_articles        — cached article metadata (no full text; 500-char snippet)
--   news_article_symbols — many-to-many article<->ticker
--   news_feed_config     — single-row user config for the general-market feed
--   news_alerts_sent     — dedup so push notifications fire once per article/channel
--
-- FTS uses a GENERATED ALWAYS AS tsvector so updates to title/snippet
-- automatically keep the index fresh.

CREATE TABLE news_articles (
    article_id          TEXT        PRIMARY KEY,  -- hash of link or EODHD article id
    published_at        TIMESTAMPTZ NOT NULL,
    title               TEXT        NOT NULL,
    snippet             TEXT,                     -- first ~500 chars of content
    link                TEXT        NOT NULL,
    source              TEXT,                     -- 'reuters', 'bloomberg', etc.
    sentiment_polarity  NUMERIC(5,4),             -- -1..+1
    sentiment_pos       NUMERIC(5,4),
    sentiment_neg       NUMERIC(5,4),
    sentiment_neu       NUMERIC(5,4),
    tags                TEXT[]      NOT NULL DEFAULT '{}',
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    search_vec          TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title, '') || ' ' || coalesce(snippet, ''))
    ) STORED
);

CREATE INDEX idx_news_published ON news_articles (published_at DESC);
CREATE INDEX idx_news_tags      ON news_articles USING GIN (tags);
CREATE INDEX idx_news_fts       ON news_articles USING GIN (search_vec);

-- Many-to-many: one article can mention multiple symbols.
CREATE TABLE news_article_symbols (
    article_id  TEXT NOT NULL REFERENCES news_articles(article_id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    PRIMARY KEY (article_id, symbol)
);

CREATE INDEX idx_news_sym_lookup ON news_article_symbols (symbol, article_id);

-- Single-row config (id is always 1; enforced by CHECK).
CREATE TABLE news_feed_config (
    id                      INT         PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    symbols                 TEXT[]      NOT NULL DEFAULT '{}',
    tags                    TEXT[]      NOT NULL DEFAULT '{}',
    sentiment_alert_enabled BOOL        NOT NULL DEFAULT FALSE,
    sentiment_threshold     NUMERIC(4,2) NOT NULL DEFAULT 0.70,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed the general-market feed with the curated defaults from the TODO.
INSERT INTO news_feed_config (id, symbols, tags) VALUES (
    1,
    ARRAY[
        'SPY','QQQ','DIA','IWM',           -- broad-market indices / ETFs
        'XLK','SOXX','SMH',                -- sector ETFs (tech/semis)
        'AAPL','MSFT','NVDA','GOOGL',      -- mega-cap tech anchors
        'META','AMZN','AMD','TSM','ASML'
    ],
    ARRAY[
        'monetary-policy',
        'economic-indicators',
        'earnings',
        'artificial-intelligence'
    ]
) ON CONFLICT (id) DO NOTHING;

-- Dedup table so each (article, channel) pair fires at most once.
CREATE TABLE news_alerts_sent (
    article_id  TEXT        NOT NULL REFERENCES news_articles(article_id) ON DELETE CASCADE,
    channel     TEXT        NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (article_id, channel)
);
