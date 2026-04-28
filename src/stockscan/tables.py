"""SQLAlchemy Core Table objects matching the Alembic schema.

Used for type-safe query construction throughout the codebase. The
authoritative schema lives in alembic/versions/0001_initial_schema.py;
this module mirrors it for code-side use.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP

metadata = MetaData()

# ----------------------------------------------------------------------
# Accounts
# ----------------------------------------------------------------------
accounts = Table(
    "accounts",
    metadata,
    Column("account_id", BigInteger, primary_key=True),
    Column("broker", Text, nullable=False),
    Column("broker_account_id", Text),
    Column("label", Text),
    Column("account_type", Text, nullable=False),
    Column("base_currency", Text, nullable=False, server_default="'USD'"),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"),
    CheckConstraint("account_type IN ('taxable','ira','roth','paper')"),
)

# ----------------------------------------------------------------------
# Reference data
# ----------------------------------------------------------------------
universe_history = Table(
    "universe_history",
    metadata,
    Column("symbol", Text, nullable=False),
    Column("joined_date", Date, nullable=False),
    Column("left_date", Date),
    PrimaryKeyConstraint("symbol", "joined_date"),
)

corporate_actions = Table(
    "corporate_actions",
    metadata,
    Column("symbol", Text, nullable=False),
    Column("action_date", Date, nullable=False),
    Column("action_type", Text, nullable=False),
    Column("ratio", Numeric(20, 10)),
    Column("amount", Numeric(20, 6)),
    Column("raw_payload", JSONB),
    PrimaryKeyConstraint("symbol", "action_date", "action_type"),
    CheckConstraint("action_type IN ('split','cash_div','stock_div','spinoff')"),
)

earnings_calendar = Table(
    "earnings_calendar",
    metadata,
    Column("symbol", Text, nullable=False),
    Column("report_date", Date, nullable=False),
    Column("time_of_day", Text),
    Column("estimate", Numeric(12, 4)),
    Column("actual", Numeric(12, 4)),
    Column("fetched_at", TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"),
    PrimaryKeyConstraint("symbol", "report_date"),
    CheckConstraint("time_of_day IN ('bmo','amc','unknown')"),
)
Index("idx_earnings_date", earnings_calendar.c.report_date)

# ----------------------------------------------------------------------
# Bars (TimescaleDB hypertable)
# ----------------------------------------------------------------------
bars = Table(
    "bars",
    metadata,
    Column("symbol", Text, nullable=False),
    Column("bar_ts", TIMESTAMP(timezone=True), nullable=False),
    Column("interval", Text, nullable=False, server_default="'1d'"),
    Column("open", Numeric(14, 6), nullable=False),
    Column("high", Numeric(14, 6), nullable=False),
    Column("low", Numeric(14, 6), nullable=False),
    Column("close", Numeric(14, 6), nullable=False),
    Column("adj_close", Numeric(14, 6), nullable=False),
    Column("volume", BigInteger, nullable=False),
    Column("source", Text, nullable=False, server_default="'eodhd'"),
    Column("fetched_at", TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"),
    PrimaryKeyConstraint("symbol", "interval", "bar_ts"),
)

# ----------------------------------------------------------------------
# Strategy plugin tables
# ----------------------------------------------------------------------
strategy_versions = Table(
    "strategy_versions",
    metadata,
    Column("strategy_name", Text, nullable=False),
    Column("strategy_version", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("description", Text),
    Column("tags", ARRAY(Text), nullable=False, server_default="'{}'"),
    Column("params_json_schema", JSONB, nullable=False),
    Column("code_fingerprint", Text, nullable=False),
    Column("first_seen_at", TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"),
    PrimaryKeyConstraint("strategy_name", "strategy_version"),
)

strategy_configs = Table(
    "strategy_configs",
    metadata,
    Column("config_id", BigInteger, primary_key=True),
    Column("strategy_name", Text, nullable=False),
    Column("strategy_version", Text, nullable=False),
    Column("params_json", JSONB, nullable=False),
    Column("params_hash", Text, nullable=False),
    Column("risk_pct_override", Numeric(5, 4)),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"),
    Column("created_by", Text),
    Column("note", Text),
    ForeignKeyConstraint(
        ["strategy_name", "strategy_version"],
        ["strategy_versions.strategy_name", "strategy_versions.strategy_version"],
    ),
)

strategy_runs = Table(
    "strategy_runs",
    metadata,
    Column("run_id", BigInteger, primary_key=True),
    Column("strategy_name", Text, nullable=False),
    Column("strategy_version", Text, nullable=False),
    Column("config_id", BigInteger, ForeignKey("strategy_configs.config_id"), nullable=False),
    Column("run_at", TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"),
    Column("as_of_date", Date, nullable=False),
    Column("universe_size", Integer, nullable=False),
    Column("signals_emitted", Integer, nullable=False),
    Column("rejected_count", Integer, nullable=False, server_default="0"),
    ForeignKeyConstraint(
        ["strategy_name", "strategy_version"],
        ["strategy_versions.strategy_name", "strategy_versions.strategy_version"],
    ),
)

# ----------------------------------------------------------------------
# Signals
# ----------------------------------------------------------------------
signals = Table(
    "signals",
    metadata,
    Column("signal_id", BigInteger, primary_key=True),
    Column("run_id", BigInteger, ForeignKey("strategy_runs.run_id")),
    Column("strategy_name", Text, nullable=False),
    Column("strategy_version", Text, nullable=False),
    Column("config_id", BigInteger, ForeignKey("strategy_configs.config_id"), nullable=False),
    Column("symbol", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("score", Numeric(10, 6)),
    Column("as_of_date", Date, nullable=False),
    Column("suggested_entry", Numeric(14, 6)),
    Column("suggested_stop", Numeric(14, 6)),
    Column("suggested_target", Numeric(14, 6)),
    Column("suggested_qty", Integer),
    Column("rejected_reason", Text),
    Column("metadata", JSONB),
    Column("status", Text, nullable=False),
    CheckConstraint("side IN ('long','short')"),
    CheckConstraint("status IN ('new','ordered','rejected','expired')"),
    ForeignKeyConstraint(
        ["strategy_name", "strategy_version"],
        ["strategy_versions.strategy_name", "strategy_versions.strategy_version"],
    ),
)
Index("idx_signals_status_date", signals.c.status, signals.c.as_of_date)

# ----------------------------------------------------------------------
# Orders, trades, lots, sales
# ----------------------------------------------------------------------
orders = Table(
    "orders",
    metadata,
    Column("order_id", BigInteger, primary_key=True),
    Column("account_id", BigInteger, ForeignKey("accounts.account_id"), nullable=False),
    Column("signal_id", BigInteger, ForeignKey("signals.signal_id")),
    Column("broker_order_id", Text),
    Column("broker", Text, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("qty", Integer, nullable=False),
    Column("order_type", Text, nullable=False),
    Column("limit_price", Numeric(14, 6)),
    Column("stop_price", Numeric(14, 6)),
    Column("status", Text, nullable=False),
    Column("submitted_at", TIMESTAMP(timezone=True)),
    Column("filled_at", TIMESTAMP(timezone=True)),
    Column("avg_fill_price", Numeric(14, 6)),
    Column("commission", Numeric(10, 4), nullable=False, server_default="0"),
    CheckConstraint("side IN ('buy','sell')"),
)

trades = Table(
    "trades",
    metadata,
    Column("trade_id", BigInteger, primary_key=True),
    Column("account_id", BigInteger, ForeignKey("accounts.account_id"), nullable=False),
    Column("symbol", Text, nullable=False),
    Column("strategy", Text, nullable=False),
    Column("entry_signal_id", BigInteger, ForeignKey("signals.signal_id")),
    Column("opened_at", TIMESTAMP(timezone=True), nullable=False),
    Column("closed_at", TIMESTAMP(timezone=True)),
    Column("status", Text, nullable=False),
    Column("realized_pnl", Numeric(14, 4)),
    Column("holding_days", Integer),
    Column("max_favorable_excursion", Numeric(8, 4)),
    Column("max_adverse_excursion", Numeric(8, 4)),
    CheckConstraint("status IN ('open','closed')"),
)

tax_lots = Table(
    "tax_lots",
    metadata,
    Column("lot_id", BigInteger, primary_key=True),
    Column("account_id", BigInteger, ForeignKey("accounts.account_id"), nullable=False),
    Column("trade_id", BigInteger, ForeignKey("trades.trade_id"), nullable=False),
    Column("symbol", Text, nullable=False),
    Column("strategy", Text, nullable=False),
    Column("qty_original", Integer, nullable=False),
    Column("qty_remaining", Integer, nullable=False),
    Column("cost_basis", Numeric(14, 6), nullable=False),
    Column("acquired_at", TIMESTAMP(timezone=True), nullable=False),
    Column("source_order_id", BigInteger, ForeignKey("orders.order_id")),
    Column("closed_at", TIMESTAMP(timezone=True)),
    CheckConstraint("qty_remaining >= 0"),
)

lot_sales = Table(
    "lot_sales",
    metadata,
    Column("sale_id", BigInteger, primary_key=True),
    Column("sell_order_id", BigInteger, ForeignKey("orders.order_id"), nullable=False),
    Column("lot_id", BigInteger, ForeignKey("tax_lots.lot_id"), nullable=False),
    Column("qty_sold", Integer, nullable=False),
    Column("sale_price", Numeric(14, 6), nullable=False),
    Column("sold_at", TIMESTAMP(timezone=True), nullable=False),
    Column("realized_pnl", Numeric(14, 4), nullable=False),
    Column("holding_period_days", Integer, nullable=False),
)

# ----------------------------------------------------------------------
# NAV / suggestions / notes
# ----------------------------------------------------------------------
equity_history = Table(
    "equity_history",
    metadata,
    Column("account_id", BigInteger, ForeignKey("accounts.account_id"), nullable=False),
    Column("as_of_date", Date, nullable=False),
    Column("cash", Numeric(16, 4), nullable=False),
    Column("positions_value", Numeric(16, 4), nullable=False),
    Column("total_equity", Numeric(16, 4), nullable=False),
    Column("high_water_mark", Numeric(16, 4), nullable=False),
    PrimaryKeyConstraint("account_id", "as_of_date"),
)

suggestions = Table(
    "suggestions",
    metadata,
    Column("suggestion_id", BigInteger, primary_key=True),
    Column("account_id", BigInteger, ForeignKey("accounts.account_id"), nullable=False),
    Column("signal_id", BigInteger, ForeignKey("signals.signal_id"), nullable=False),
    Column("suggested_at", TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"),
    Column("action", Text, nullable=False),
    Column("qty", Integer, nullable=False),
    Column("user_action", Text, nullable=False, server_default="'pending'"),
    Column("user_action_at", TIMESTAMP(timezone=True)),
    Column("journal_notes", Text),
    CheckConstraint("user_action IN ('taken','skipped','pending')"),
)

trade_notes = Table(
    "trade_notes",
    metadata,
    Column("note_id", BigInteger, primary_key=True),
    Column(
        "trade_id",
        BigInteger,
        ForeignKey("trades.trade_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"),
    Column("note_type", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("template_fields", JSONB),
    # body_tsv is GENERATED ALWAYS — not declared on the application side; queries
    # use it via raw SQL or text() expressions.
    CheckConstraint("note_type IN ('entry','mid','exit','free')"),
)

trade_note_revisions = Table(
    "trade_note_revisions",
    metadata,
    Column("revision_id", BigInteger, primary_key=True),
    Column(
        "note_id",
        BigInteger,
        ForeignKey("trade_notes.note_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("body_before", Text, nullable=False),
    Column("template_fields_before", JSONB),
    Column("edited_at", TIMESTAMP(timezone=True), nullable=False, server_default="NOW()"),
)
