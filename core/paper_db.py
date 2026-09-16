"""
SQLite persistence for paper trading state and instrument caching.

Tables:
  - balances:          Virtual balance per exchange (survives restarts)
  - positions:         Open virtual positions
  - trades:            Closed trade history with P&L
  - instruments_cache: Cached instrument lists per exchange (avoid re-fetching)
  - kv_store:          Generic key-value for session metadata

DB file: data/paper_trading.db (auto-created)
"""

import os
import json
import sqlite3
import time
import logging
from contextlib import contextmanager
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

from exchanges.base import InstrumentInfo

log = logging.getLogger("paper_db")

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "paper_trading.db")


def _ensure_dir():
    os.makedirs(DB_DIR, exist_ok=True)


class PaperDB:
    """SQLite persistence for paper trading state and instrument cache."""

    def __init__(self, db_path: str = DB_PATH):
        _ensure_dir()
        self._db_path = db_path
        self._init_schema()
        log.info("PaperDB initialized at %s", db_path)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS balances (
                    exchange        TEXT PRIMARY KEY,
                    balance         REAL NOT NULL,
                    initial_balance REAL NOT NULL,
                    total_fees_paid REAL NOT NULL DEFAULT 0.0,
                    updated_at      REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS positions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange        TEXT NOT NULL,
                    symbol          TEXT NOT NULL,
                    side            TEXT NOT NULL,
                    quantity        REAL NOT NULL,
                    entry_price     REAL NOT NULL,
                    leverage        INTEGER NOT NULL DEFAULT 10,
                    opened_at       REAL NOT NULL,
                    UNIQUE(exchange, symbol)
                );

                CREATE TABLE IF NOT EXISTS trades (
                    trade_id        TEXT PRIMARY KEY,
                    exchange        TEXT NOT NULL,
                    symbol          TEXT NOT NULL,
                    side            TEXT NOT NULL,
                    quantity        REAL NOT NULL,
                    entry_price     REAL NOT NULL,
                    exit_price      REAL NOT NULL DEFAULT 0.0,
                    pnl             REAL NOT NULL DEFAULT 0.0,
                    funding_earned  REAL NOT NULL DEFAULT 0.0,
                    opened_at       REAL NOT NULL,
                    closed_at       REAL NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS instruments_cache (
                    exchange                TEXT NOT NULL,
                    symbol                  TEXT NOT NULL,
                    base_asset              TEXT NOT NULL,
                    quote_asset             TEXT NOT NULL,
                    contract_type           TEXT NOT NULL,
                    min_quantity            REAL NOT NULL DEFAULT 0.0,
                    tick_size               REAL NOT NULL DEFAULT 0.0,
                    is_active               INTEGER NOT NULL DEFAULT 1,
                    funding_interval_minutes INTEGER,
                    cached_at               REAL NOT NULL,
                    PRIMARY KEY (exchange, symbol)
                );

                CREATE TABLE IF NOT EXISTS kv_store (
                    key     TEXT PRIMARY KEY,
                    value   TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_trades_exchange
                    ON trades(exchange);
                CREATE INDEX IF NOT EXISTS idx_instruments_exchange
                    ON instruments_cache(exchange);
                CREATE INDEX IF NOT EXISTS idx_positions_exchange
                    ON positions(exchange);
            """)

    # ------------------------------------------------------------------
    # Balance persistence
    # ------------------------------------------------------------------

    def save_balance(self, exchange: str, balance: float,
                     initial_balance: float, total_fees_paid: float):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO balances (exchange, balance, initial_balance, total_fees_paid, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(exchange) DO UPDATE SET
                    balance = excluded.balance,
                    total_fees_paid = excluded.total_fees_paid,
                    updated_at = excluded.updated_at
            """, (exchange, balance, initial_balance, total_fees_paid, time.time()))

    def load_balance(self, exchange: str) -> Optional[Dict]:
        """Returns dict with balance, initial_balance, total_fees_paid or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT balance, initial_balance, total_fees_paid FROM balances WHERE exchange = ?",
                (exchange,)
            ).fetchone()
            if row:
                return {
                    "balance": row["balance"],
                    "initial_balance": row["initial_balance"],
                    "total_fees_paid": row["total_fees_paid"],
                }
        return None

    # ------------------------------------------------------------------
    # Position persistence
    # ------------------------------------------------------------------

    def save_position(self, exchange: str, symbol: str, side: str,
                      quantity: float, entry_price: float, leverage: int):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO positions (exchange, symbol, side, quantity, entry_price, leverage, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, symbol) DO UPDATE SET
                    side = excluded.side,
                    quantity = excluded.quantity,
                    entry_price = excluded.entry_price,
                    leverage = excluded.leverage
            """, (exchange, symbol, side, quantity, entry_price, leverage, time.time()))

    def remove_position(self, exchange: str, symbol: str):
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM positions WHERE exchange = ? AND symbol = ?",
                (exchange, symbol)
            )

    def load_positions(self, exchange: str) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT symbol, side, quantity, entry_price, leverage, opened_at "
                "FROM positions WHERE exchange = ?",
                (exchange,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Trade history
    # ------------------------------------------------------------------

    def save_trade(self, trade_id: str, exchange: str, symbol: str, side: str,
                   quantity: float, entry_price: float, exit_price: float = 0.0,
                   pnl: float = 0.0, funding_earned: float = 0.0,
                   opened_at: float = 0.0, closed_at: float = 0.0):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO trades (trade_id, exchange, symbol, side, quantity,
                                    entry_price, exit_price, pnl, funding_earned,
                                    opened_at, closed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    exit_price = excluded.exit_price,
                    pnl = excluded.pnl,
                    funding_earned = excluded.funding_earned,
                    closed_at = excluded.closed_at
            """, (trade_id, exchange, symbol, side, quantity, entry_price,
                  exit_price, pnl, funding_earned, opened_at, closed_at))

    def load_trades(self, exchange: str) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exchange = ? ORDER BY opened_at",
                (exchange,)
            ).fetchall()
            return [dict(r) for r in rows]

    def load_open_trades(self, exchange: str) -> List[Dict]:
        """Trades where exit_price is 0 (still open)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exchange = ? AND exit_price = 0.0 "
                "ORDER BY opened_at",
                (exchange,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_trade_stats(self, exchange: str = None) -> Dict:
        """Aggregate trade statistics."""
        where = "WHERE exchange = ?" if exchange else ""
        params = (exchange,) if exchange else ()
        with self._conn() as conn:
            row = conn.execute(f"""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN closed_at > 0 THEN 1 ELSE 0 END) as closed_trades,
                    SUM(CASE WHEN closed_at > 0 AND pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN closed_at > 0 AND pnl <= 0 THEN 1 ELSE 0 END) as losses,
                    COALESCE(SUM(CASE WHEN closed_at > 0 THEN pnl ELSE 0 END), 0) as total_pnl,
                    COALESCE(SUM(funding_earned), 0) as total_funding
                FROM trades {where}
            """, params).fetchone()
            return dict(row) if row else {}

    # ------------------------------------------------------------------
    # Instrument cache
    # ------------------------------------------------------------------

    def save_instruments(self, exchange: str, instruments: List[InstrumentInfo]):
        """Cache instrument list for an exchange. Replaces previous cache."""
        now = time.time()
        with self._conn() as conn:
            # Clear old cache for this exchange
            conn.execute("DELETE FROM instruments_cache WHERE exchange = ?", (exchange,))
            # Insert new
            for inst in instruments:
                conn.execute("""
                    INSERT INTO instruments_cache
                        (exchange, symbol, base_asset, quote_asset, contract_type,
                         min_quantity, tick_size, is_active, funding_interval_minutes, cached_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (exchange, inst.symbol, inst.base_asset, inst.quote_asset,
                      inst.contract_type, inst.min_quantity, inst.tick_size,
                      1 if inst.is_active else 0, inst.funding_interval_minutes, now))
        log.info("Cached %d instruments for %s", len(instruments), exchange)

    def load_instruments(self, exchange: str,
                          max_age_sec: float = 86400) -> Optional[List[InstrumentInfo]]:
        """Load cached instruments if they are fresher than max_age_sec.
        Returns None if cache is stale or empty."""
        cutoff = time.time() - max_age_sec
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM instruments_cache WHERE exchange = ? AND cached_at > ?",
                (exchange, cutoff)
            ).fetchall()
            if not rows:
                return None
            return [
                InstrumentInfo(
                    symbol=r["symbol"],
                    base_asset=r["base_asset"],
                    quote_asset=r["quote_asset"],
                    contract_type=r["contract_type"],
                    min_quantity=r["min_quantity"],
                    tick_size=r["tick_size"],
                    is_active=bool(r["is_active"]),
                    funding_interval_minutes=r["funding_interval_minutes"],
                )
                for r in rows
            ]

    def get_instruments_cache_age(self, exchange: str) -> Optional[float]:
        """Returns seconds since instruments were last cached, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(cached_at) as latest FROM instruments_cache WHERE exchange = ?",
                (exchange,)
            ).fetchone()
            if row and row["latest"]:
                return time.time() - row["latest"]
        return None

    # ------------------------------------------------------------------
    # Key-value store (session metadata)
    # ------------------------------------------------------------------

    def set_kv(self, key: str, value: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO kv_store (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value)
            )

    def get_kv(self, key: str, default: str = None) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM kv_store WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def clear_all(self):
        """Reset everything — use with caution."""
        with self._conn() as conn:
            conn.executescript("""
                DELETE FROM balances;
                DELETE FROM positions;
                DELETE FROM trades;
                DELETE FROM instruments_cache;
                DELETE FROM kv_store;
            """)
        log.warning("PaperDB cleared completely")


# Module-level singleton
paper_db = PaperDB()
