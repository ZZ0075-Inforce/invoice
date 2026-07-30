"""SQLite 儲存層。所有寫入都是 upsert，重跑不會產生重複資料。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DB = Path("out/twcrawl.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    inv_num      TEXT PRIMARY KEY,
    inv_date     TEXT,
    seller_name  TEXT,
    seller_ban   TEXT,
    amount       REAL,
    card_type    TEXT,
    card_no      TEXT,
    inv_status   TEXT,
    inv_period   TEXT,
    donatable    TEXT,
    source       TEXT,
    raw          TEXT,
    fetched_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(inv_date);

CREATE TABLE IF NOT EXISTS invoice_items (
    inv_num      TEXT NOT NULL,
    row_no       INTEGER NOT NULL,
    description  TEXT,
    quantity     REAL,
    unit_price   REAL,
    amount       REAL,
    raw          TEXT,
    PRIMARY KEY (inv_num, row_no)
);

-- 財政部稅籍登記對照（bizreg 指令維護；只存發票出現過的統編）
CREATE TABLE IF NOT EXISTS biz_registry (
    ban            TEXT PRIMARY KEY,
    name           TEXT,
    address        TEXT,
    industry       TEXT,
    industry_codes TEXT,
    fetched_at     TEXT DEFAULT (datetime('now'))
);

-- 統一發票中獎號碼（lottery 指令維護；期別＝民國年＋起始月，如 11505）
CREATE TABLE IF NOT EXISTS lottery_draws (
    period      TEXT PRIMARY KEY,
    special     TEXT,   -- 特別獎 8 碼（1,000 萬）
    grand       TEXT,   -- 特獎 8 碼（200 萬）
    first_json  TEXT,   -- 頭獎號碼 JSON 陣列（衍生二～六獎）
    extra_json  TEXT,   -- 增開六獎末 3 碼 JSON 陣列
    claim_start TEXT,   -- 領獎起訖（西元 ISO）
    claim_end   TEXT,
    fetched_at  TEXT DEFAULT (datetime('now'))
);

-- FDA 的欄位事前無法確定，用 JSON 存整列，並以內容雜湊去重。
CREATE TABLE IF NOT EXISTS fda_rows (
    row_hash     TEXT PRIMARY KEY,
    source_url   TEXT,
    table_key    TEXT,
    page_no      INTEGER,
    data         TEXT,
    first_seen   TEXT DEFAULT (datetime('now')),
    last_seen    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fda_table ON fda_rows(table_key);
"""


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """既有資料庫補新欄位（CREATE IF NOT EXISTS 不會動到既存表）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(biz_registry)")}
    for col, decl in (("lat", "REAL"), ("lon", "REAL"), ("geocoded_at", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE biz_registry ADD COLUMN {col} {decl}")
    conn.commit()


def upsert_invoices(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    sql = """
    INSERT INTO invoices (inv_num, inv_date, seller_name, seller_ban, amount,
                          card_type, card_no, inv_status, inv_period, donatable,
                          source, raw)
    VALUES (:inv_num, :inv_date, :seller_name, :seller_ban, :amount,
            :card_type, :card_no, :inv_status, :inv_period, :donatable,
            :source, :raw)
    ON CONFLICT(inv_num) DO UPDATE SET
        inv_date    = COALESCE(excluded.inv_date, inv_date),
        seller_name = COALESCE(excluded.seller_name, seller_name),
        seller_ban  = COALESCE(excluded.seller_ban, seller_ban),
        amount      = COALESCE(excluded.amount, amount),
        inv_status  = COALESCE(excluded.inv_status, inv_status),
        raw         = excluded.raw
    """
    n = 0
    for r in rows:
        conn.execute(sql, {k: r.get(k) for k in _INVOICE_KEYS})
        n += 1
    conn.commit()
    return n


_INVOICE_KEYS = (
    "inv_num inv_date seller_name seller_ban amount card_type card_no "
    "inv_status inv_period donatable source raw"
).split()


def upsert_items(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    sql = """
    INSERT INTO invoice_items (inv_num, row_no, description, quantity,
                               unit_price, amount, raw)
    VALUES (:inv_num, :row_no, :description, :quantity, :unit_price, :amount, :raw)
    ON CONFLICT(inv_num, row_no) DO UPDATE SET
        description = excluded.description,
        quantity    = excluded.quantity,
        unit_price  = excluded.unit_price,
        amount      = excluded.amount,
        raw         = excluded.raw
    """
    n = 0
    for r in rows:
        conn.execute(
            sql,
            {
                k: r.get(k)
                for k in ("inv_num row_no description quantity unit_price amount raw").split()
            },
        )
        n += 1
    conn.commit()
    return n


def upsert_lottery_draws(conn: sqlite3.Connection, draws: Iterable[dict[str, Any]]) -> int:
    sql = """
    INSERT INTO lottery_draws (period, special, grand, first_json, extra_json,
                               claim_start, claim_end)
    VALUES (:period, :special, :grand, :first_json, :extra_json,
            :claim_start, :claim_end)
    ON CONFLICT(period) DO UPDATE SET
        special     = excluded.special,
        grand       = excluded.grand,
        first_json  = excluded.first_json,
        extra_json  = excluded.extra_json,
        claim_start = excluded.claim_start,
        claim_end   = excluded.claim_end,
        fetched_at  = datetime('now')
    """
    n = 0
    for d in draws:
        payload = dict(d)
        payload["first_json"] = json.dumps(payload.pop("first", []))
        payload["extra_json"] = json.dumps(payload.pop("extra", []))
        conn.execute(sql, {k: payload.get(k) for k in (
            "period special grand first_json extra_json claim_start claim_end"
        ).split()})
        n += 1
    conn.commit()
    return n


def upsert_fda_rows(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    sql = """
    INSERT INTO fda_rows (row_hash, source_url, table_key, page_no, data)
    VALUES (:row_hash, :source_url, :table_key, :page_no, :data)
    ON CONFLICT(row_hash) DO UPDATE SET last_seen = datetime('now')
    """
    n = 0
    for r in rows:
        payload = dict(r)
        if not isinstance(payload.get("data"), str):
            payload["data"] = json.dumps(payload.get("data"), ensure_ascii=False)
        conn.execute(sql, payload)
        n += 1
    conn.commit()
    return n
