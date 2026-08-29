"""
storage.py
SQLite persistence for tracked Flipkart products.

NOTE ON RENDER FREE TIER: the free tier's filesystem is EPHEMERAL —
it resets on every deploy/restart. This is fine for testing, but for
real 24/7 use you either need:
  - Render's paid "Persistent Disk" add-on, OR
  - swap this for a free external DB (e.g. Supabase/Neon Postgres free tier)
See README.md for details.
"""

import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = "tracker.db"
_lock = threading.Lock()


def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                last_price REAL,
                target_price REAL,
                bajaj_emi_available INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                alert_active INTEGER DEFAULT 0
            )
        """)
        conn.commit()


@contextmanager
def _get_conn():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def add_product(chat_id, url, target_price):
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO products (chat_id, url, target_price) VALUES (?, ?, ?)",
            (chat_id, url, target_price),
        )
        conn.commit()
        return cur.lastrowid


def list_products(chat_id):
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM products WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_products():
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM products").fetchall()
        return [dict(r) for r in rows]


def get_product(product_id):
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        return dict(row) if row else None


def remove_product(product_id, chat_id):
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM products WHERE id = ? AND chat_id = ?", (product_id, chat_id)
        )
        conn.commit()
        return cur.rowcount > 0


def update_price(product_id, new_price, title, bajaj_available):
    with _get_conn() as conn:
        conn.execute(
            "UPDATE products SET last_price = ?, title = ?, bajaj_emi_available = ? WHERE id = ?",
            (new_price, title, int(bajaj_available), product_id),
        )
        conn.commit()


def set_alert_active(product_id, active: bool):
    with _get_conn() as conn:
        conn.execute(
            "UPDATE products SET alert_active = ? WHERE id = ?",
            (int(active), product_id),
        )
        conn.commit()
