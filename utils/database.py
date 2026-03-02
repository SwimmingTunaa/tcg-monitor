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
                CREATE TABLE IF NOT EXISTS canonical_products (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    set_key TEXT NOT NULL,
                    type TEXT NOT NULL,
                    tcg TEXT NOT NULL DEFAULT 'pokemon',
                    msrp REAL,
                    image TEXT,
                    release_date TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

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
                    last_changed TEXT NOT NULL,
                    canonical_id TEXT,
                    match_status TEXT DEFAULT 'unmatched',
                    FOREIGN KEY (canonical_id) REFERENCES canonical_products(id)
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

                CREATE INDEX IF NOT EXISTS idx_product_status_canonical
                    ON product_status(canonical_id);

                CREATE INDEX IF NOT EXISTS idx_product_status_match
                    ON product_status(match_status);
            """)

            # Migrate existing DB — add new columns if they don't exist yet
            self._migrate(conn)

            conn.commit()
            logger.info(f"Database initialized at {self.db_path}")
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection):
        """Add new columns to existing tables if upgrading from an older schema."""
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(product_status)")
        }
        if "canonical_id" not in existing_cols:
            conn.execute("ALTER TABLE product_status ADD COLUMN canonical_id TEXT")
            logger.info("Migrated: added canonical_id to product_status")
        if "match_status" not in existing_cols:
            conn.execute("ALTER TABLE product_status ADD COLUMN match_status TEXT DEFAULT 'unmatched'")
            logger.info("Migrated: added match_status to product_status")
        if "sku" not in existing_cols:
            conn.execute("ALTER TABLE product_status ADD COLUMN sku TEXT")
            logger.info("Migrated: added sku to product_status")

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
                      sku: Optional[str] = None,
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
                     stock_text, image_url, sku, last_checked, last_changed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    name = excluded.name,
                    retailer = excluded.retailer,
                    in_stock = excluded.in_stock,
                    price = excluded.price,
                    price_str = excluded.price_str,
                    stock_text = excluded.stock_text,
                    image_url = excluded.image_url,
                    sku = COALESCE(excluded.sku, product_status.sku),
                    last_checked = excluded.last_checked,
                    last_changed = ?
            """, (url, name, retailer, int(in_stock), price, price_str,
                  stock_text, image_url, sku, now, now, last_changed))
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

    # ─── Canonical Products ─────────────────────────────────────────

    def upsert_canonical(self, id: str, name: str, set_key: str, type: str,
                         tcg: str = "pokemon", msrp: Optional[float] = None,
                         image: Optional[str] = None,
                         release_date: Optional[str] = None) -> bool:
        """Insert or update a canonical product. Returns True if new."""
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT id FROM canonical_products WHERE id = ?", (id,)
            ).fetchone()

            conn.execute("""
                INSERT INTO canonical_products
                    (id, name, set_key, type, tcg, msrp, image, release_date, active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    set_key = excluded.set_key,
                    type = excluded.type,
                    msrp = excluded.msrp,
                    image = excluded.image,
                    release_date = excluded.release_date,
                    active = 1
            """, (id, name, set_key, type, tcg, msrp, image,
                  release_date, datetime.now().isoformat()))
            conn.commit()
            return existing is None  # True if newly inserted
        finally:
            conn.close()

    def get_canonical(self, id: str) -> Optional[dict]:
        """Get a canonical product by ID."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM canonical_products WHERE id = ?", (id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_all_canonical(self, tcg: str = None, active_only: bool = True) -> list[dict]:
        """Get all canonical products, optionally filtered by TCG."""
        conn = self._get_conn()
        try:
            query = "SELECT * FROM canonical_products WHERE 1=1"
            params = []
            if active_only:
                query += " AND active = 1"
            if tcg:
                query += " AND tcg = ?"
                params.append(tcg)
            query += " ORDER BY set_key, type"
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def set_canonical_match(self, url: str, canonical_id: Optional[str],
                            match_status: str):
        """Update the canonical match for a product_status row."""
        conn = self._get_conn()
        try:
            conn.execute("""
                UPDATE product_status
                SET canonical_id = ?, match_status = ?
                WHERE url = ?
            """, (canonical_id, match_status, url))
            conn.commit()
        finally:
            conn.close()

    def get_unmatched(self, retailer: str = None) -> list[dict]:
        """Get all product_status rows flagged as unmatched or review."""
        conn = self._get_conn()
        try:
            query = """
                SELECT * FROM product_status
                WHERE match_status IN ('unmatched', 'review')
            """
            params = []
            if retailer:
                query += " AND retailer = ?"
                params.append(retailer)
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
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
