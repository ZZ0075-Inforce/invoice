"""SQLite 儲存層。所有寫入都是 upsert，重跑不會產生重複資料。

讀取面只收「不只一個 caller 要」的那幾種：發票與品項的過濾讀取（export／
lottery／match 共用同一份條件）、全庫筆數與最新日期（update 與 restore 都要）、
稅籍登記的讀寫。一次性的聚合查詢（FDA 來源統計、常買品項 top3）留在原地——
搬進來只是把 SQL 換個地方放。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, NamedTuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    -- NOT NULL 不是裝飾：SQLite 的 NULL 不受主鍵唯一性約束（理由見 _require_key，
    -- invoice_items 本來就宣告了）。既存的資料庫不會被這行補上——CREATE IF NOT
    -- EXISTS 不動既存表，所以真正擋住所有資料庫的是 _require_key。
    inv_num      TEXT PRIMARY KEY NOT NULL,
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


def connect(path: Path | str) -> sqlite3.Connection:
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


def seller_industries(conn: sqlite3.Connection) -> dict[str, str]:
    """店家名 → 稅籍行業名稱（供 Classifier 的行業後備層用）。

    每家店一個答案：統編取該店家名下任一非空的（SQLite 的 max 忽略 NULL）。
    半數發票不帶統編，逐張查會讓同一家店在不同發票上得到不同分類——
    這裡收斂成一次查詢，export 與 match 共用，兩邊不可能再分岔。
    """
    return {name: industry for name, industry in conn.execute(
        "select v.seller_name, r.industry from ("
        "  select seller_name, max(seller_ban) as ban from invoices"
        "  where seller_name is not null group by seller_name) v"
        " join biz_registry r on r.ban = trim(v.ban)"
        " where r.industry is not null")}


class Invoice(NamedTuple):
    """一張發票的表頭。`date` 保證非空——三個讀取端都靠它定位（export 分月、
    lottery 比期別、match 比 since），所以那道保護在 invoices() 裡面。"""
    num: str
    date: str
    seller: str | None
    amount: float | None


class Item(NamedTuple):
    """一列品項，帶著所屬發票的日期與店家——比對與分月都要，而且過濾條件
    本來就下在發票上。"""
    inv_num: str
    date: str
    seller: str | None
    row_no: int | None
    desc: str | None
    qty: float | None
    price: float | None
    amount: float | None


class Stats(NamedTuple):
    """庫內有多少東西、資料新到哪一天。`last` 在空庫是 None。"""
    invoices: int
    items: int
    last: str | None


def stats(conn: sqlite3.Connection) -> Stats:
    """全庫的筆數與最新發票日期。

    `max(inv_date)` 本來是 update 組步驟時的一次性查詢；restore 的還原後驗證
    也要它，兩個 caller 就該收進來（本模組開頭的判準）。順帶讓 commands.py
    完全不含 SQL。
    """
    return Stats(*conn.execute(
        "select (select count(*) from invoices),"
        " (select count(*) from invoice_items),"
        " (select max(inv_date) from invoices)").fetchone())


class Biz(NamedTuple):
    ban: str
    name: str | None
    address: str | None
    industry: str | None
    lat: float | None
    lon: float | None


def _invoice_where(alias: str, since: str | None,
                   months: Iterable[str] | None) -> tuple[str, list[Any]]:
    """發票的過濾條件。

    `alias` 讓同一份條件同時能用在單表查詢與 join 查詢上——以前 match.py 是
    把單表版的字串 `.replace('inv_date', 'v.inv_date')` 成 join 版，條件一旦
    長出第二個欄位名就會出事。
    """
    cond, args = [f"{alias}.inv_date is not null"], []
    if since:
        cond.append(f"{alias}.inv_date >= ?")
        args.append(since)
    if months:
        ms = list(months)
        cond.append(f"substr({alias}.inv_date, 1, 7) in "
                    f"({','.join('?' * len(ms))})")
        args.extend(ms)
    return " and ".join(cond), args


def invoices(conn: sqlite3.Connection, *, since: str | None = None,
             months: Iterable[str] | None = None) -> list[Invoice]:
    """發票表頭，依日期升冪。

    順序是 interface 的一部分：儀表板拿最後一筆當「最新發票」算資料鮮度。
    """
    where, args = _invoice_where("invoices", since, months)
    return [Invoice(*r) for r in conn.execute(
        "select inv_num, inv_date, seller_name, amount from invoices "
        f"where {where} order by inv_date", args)]


def invoice_items(conn: sqlite3.Connection, *, since: str | None = None,
                  months: Iterable[str] | None = None) -> list[Item]:
    """品項，依發票號碼與列號升冪；過濾條件與 invoices() 同一份實作。"""
    where, args = _invoice_where("v", since, months)
    return [Item(*r) for r in conn.execute(
        "select i.inv_num, v.inv_date, v.seller_name, i.row_no, i.description,"
        " i.quantity, i.unit_price, i.amount"
        " from invoice_items i join invoices v on v.inv_num = i.inv_num"
        f" where {where} order by i.inv_num, i.row_no", args)]


def biz_registry(conn: sqlite3.Connection, *,
                 needs_geocode: bool = False) -> list[Biz]:
    """稅籍登記。needs_geocode=True 只給有地址但還沒座標的（geocode 用）。"""
    where = ("where address is not null and address != '' and lat is null"
             if needs_geocode else "")
    return [Biz(*r) for r in conn.execute(
        "select ban, name, address, industry, lat, lon "
        f"from biz_registry {where}")]


def upsert_biz(conn: sqlite3.Connection,
               rows: Iterable[dict[str, Any]]) -> int:
    """稅籍登記 upsert。座標欄不動——它由 geocode 另外寫，重抓對照表不該
    把已解出的座標洗掉。"""
    n = 0
    for r in rows:
        conn.execute(
            "insert into biz_registry (ban, name, address, industry,"
            " industry_codes) values (:ban,:name,:address,:industry,:codes)"
            " on conflict(ban) do update set name=excluded.name,"
            " address=excluded.address, industry=excluded.industry,"
            " industry_codes=excluded.industry_codes,"
            " fetched_at=datetime('now')",
            {k: r.get(k) for k in ("ban", "name", "address", "industry",
                                   "codes")})
        n += 1
    conn.commit()
    return n


def set_biz_location(conn: sqlite3.Connection, ban: str,
                     lat: float, lon: float) -> None:
    conn.execute(
        "update biz_registry set lat=?, lon=?, geocoded_at=datetime('now')"
        " where ban=?", (lat, lon, ban))


def _require_key(row: dict[str, Any], key: str, table: str) -> None:
    """主鍵欄缺值就當場停下來，不要寫出一列 NULL 主鍵。

    這裡擋的是**鍵名打錯**：upsert 是 `{k: r.get(k) for k in _INVOICE_KEYS}`，
    多餘的鍵靜靜被丟掉，於是 `{"invNum": ...}` 會寫進一列號碼是 NULL 的發票，
    而 NULL 在 SQLite 不受主鍵唯一性約束——同一列 upsert 三次就是三列，正好
    違反本模組「重跑安全」的承諾，且全程無聲。

    訊息印**鍵名**不印值：值是消費紀錄（CLAUDE.md 個資界線）。
    """
    v = row.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError(
            f"{table} 這列的 {key} 是空的；這列實際帶的鍵是 {sorted(row)}"
            f"——鍵名打錯會被 upsert 靜默丟掉，寫出 NULL 主鍵列")


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
        _require_key(r, "inv_num", "invoices")
        conn.execute(sql, {k: r.get(k) for k in _INVOICE_KEYS})
        n += 1
    conn.commit()
    return n


# 一列的鍵就是 upsert 的 interface（多的靜靜丟掉、少的存成 NULL），所以由
# test_db_row_shape_is_pinned 對「表欄位」與「測試建構器」三方釘住。
_INVOICE_KEYS = (
    "inv_num inv_date seller_name seller_ban amount card_type card_no "
    "inv_status inv_period donatable source raw"
).split()

_ITEM_KEYS = "inv_num row_no description quantity unit_price amount raw".split()


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
        # invoice_items 兩個主鍵欄都宣告了 NOT NULL，所以打錯鍵本來就會炸；
        # 這裡先擋是為了把 IntegrityError 換成指得出「你打錯哪個鍵」的訊息
        _require_key(r, "inv_num", "invoice_items")
        _require_key(r, "row_no", "invoice_items")
        conn.execute(sql, {k: r.get(k) for k in _ITEM_KEYS})
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
