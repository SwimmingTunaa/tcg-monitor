"""
SQLite database layer for persistent product tracking.

Tracks:
  - Last known stock status per product
  - Price history
  - Alert timestamps (for cooldown/deduplication)
"""
import sqlite3
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from config.settings import DB_PATH, ALERT_COOLDOWN

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS product_status (
                    url TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    retailer TEXT NOT NULL,
                    in_stock INTEGER NOT NULL DEFAULT 0,
                    price REAL,
                    price_str TEXT,
                    stock_text TEXT,
                    image_url TEXT,
                    last_checked TEXT NOT NULL,
                    last_changed TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    price REAL NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY (url) REFERENCES product_status(url)
                );

                CREATE TABLE IF NOT EXISTS alert_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    FOREIGN KEY (url) REFERENCES product_status(url)
                );

                CREATE INDEX IF NOT EXISTS idx_price_history_url
                    ON price_history(url, recorded_at);

                CREATE INDEX IF NOT EXISTS idx_alert_log_url
                    ON alert_log(url, sent_at);
            """)
            conn.commit()
            logger.info(f"Database initialized at {self.db_path}")
        finally:
            conn.close()

    # ─── Product Status ──────────────────────────────────────────────

    def get_last_status(self, url: str) -> Optional[dict]:
        """Get the last known status for a product URL."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM product_status WHERE url = ?", (url,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_status(self, url: str, name: str, retailer: str,
                      in_stock: bool, price: Optional[float] = None,
                      price_str: Optional[str] = None,
                      stock_text: Optional[str] = None,
                      image_url: Optional[str] = None,
                      status_changed: bool = False):
        """Upsert the current product status."""
        now = datetime.now().isoformat()
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT last_changed FROM product_status WHERE url = ?", (url,)
            ).fetchone()

            last_changed = now if status_changed else (
                existing["last_changed"] if existing else now
            )

            conn.execute("""
                INSERT INTO product_status
                    (url, name, retailer, in_stock, price, price_str,
                     stock_text, image_url, last_checked, last_changed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    name = excluded.name,
                    retailer = excluded.retailer,
                    in_stock = excluded.in_stock,
                    price = excluded.price,
                    price_str = excluded.price_str,
                    stock_text = excluded.stock_text,
                    image_url = excluded.image_url,
                    last_checked = excluded.last_checked,
                    last_changed = ?
            """, (url, name, retailer, int(in_stock), price, price_str,
                  stock_text, image_url, now, now, last_changed))
            conn.commit()
        finally:
            conn.close()

    # ─── Price History ───────────────────────────────────────────────

    def record_price(self, url: str, price: float):
        """Record a price point for a product."""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO price_history (url, price, recorded_at) VALUES (?, ?, ?)",
                (url, price, datetime.now().isoformat())
            )
            conn.commit()
        finally:
            conn.close()

    def get_price_history(self, url: str, days: int = 30) -> list[dict]:
        """Get price history for a product over the last N days."""
        since = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT price, recorded_at FROM price_history "
                "WHERE url = ? AND recorded_at > ? ORDER BY recorded_at",
                (url, since)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_lowest_price(self, url: str) -> Optional[float]:
        """Get the lowest recorded price for a product."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT MIN(price) as min_price FROM price_history WHERE url = ?",
                (url,)
            ).fetchone()
            return row["min_price"] if row and row["min_price"] is not None else None
        finally:
            conn.close()

    # ─── Alert Deduplication ─────────────────────────────────────────

    def can_alert(self, url: str, alert_type: str) -> bool:
        """Check if enough time has passed since the last alert for this product."""
        conn = self._get_conn()
        try:
            cutoff = (datetime.now() - timedelta(seconds=ALERT_COOLDOWN)).isoformat()
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM alert_log "
                "WHERE url = ? AND alert_type = ? AND sent_at > ?",
                (url, alert_type, cutoff)
            ).fetchone()
            return row["cnt"] == 0
        finally:
            conn.close()

    def log_alert(self, url: str, alert_type: str):
        """Record that an alert was sent."""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO alert_log (url, alert_type, sent_at) VALUES (?, ?, ?)",
                (url, alert_type, datetime.now().isoformat())
            )
            conn.commit()
        finally:
            conn.close()

    # ─── Cleanup ─────────────────────────────────────────────────────

    def cleanup_old_data(self, days: int = 90):
        """Remove price history and alert logs older than N days."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM price_history WHERE recorded_at < ?", (cutoff,))
            conn.execute("DELETE FROM alert_log WHERE sent_at < ?", (cutoff,))
            conn.commit()
            logger.info(f"Cleaned up data older than {days} days")
        finally:
            conn.close()
