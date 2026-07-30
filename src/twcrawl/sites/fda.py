"""衛福部食藥署 — 食用油業者專區下架產品清單。

https://www.fda.gov.tw/EdibleOilOperator/index.aspx

公開頁面、不需登入。實作刻意不綁定 element id：先嘗試把每頁筆數調到最大、
必要時送出查詢，再用通用分頁邏輯逐頁擷取。
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from playwright.sync_api import Page

from .. import db
from ..tables import Table, crawl_paginated, extract_tables, largest_table, row_hash

URL = "https://www.fda.gov.tw/EdibleOilOperator/index.aspx"

# 可擷取的問題商品來源。前兩者是「標題式」新聞表格（回收/下架公告都發在這），
# 比對時由 match 以「品項名稱含於標題」的模式處理。
SOURCES: dict[str, str] = {
    "edible_oil": URL,                                        # 中聯油脂案強制下架清單
    "csm_news": "https://www.fda.gov.tw/TC/csmNews.aspx",     # 國內衛生局新聞（回收/下架公告）
    "csm_light": "https://www.fda.gov.tw/TC/csmLight.aspx",   # 國外消費紅綠燈（國際回收警訊）
}

_SUBMIT_PATTERNS = ["查詢", "搜尋", "送出", "確定", "Search"]


def _to_iso_date(v) -> str | None:
    s = re.sub(r"[^\d]", "", str(v or ""))
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) == 7 and s[0] == "1":  # 民國年
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"
    return None


def _page_all_older(t: Table, since: str) -> bool:
    """該頁日期欄的最大值 < since ⇒ 清單為日期遞減，後面不用再翻。"""
    dates = [
        d
        for rec in t.rows
        for k, v in rec.items()
        if "日期" in str(k) and (d := _to_iso_date(v))
    ]
    return bool(dates) and max(dates) < since


def _maximize_page_size(page: Page) -> None:
    """把「每頁顯示筆數」的下拉選單調到最大值，減少翻頁次數。"""
    for fr in page.frames:
        try:
            selects = fr.locator("select")
            for i in range(selects.count()):
                sel = selects.nth(i)
                opts = sel.locator("option")
                values = []
                for j in range(opts.count()):
                    txt = (opts.nth(j).inner_text() or "").strip()
                    val = opts.nth(j).get_attribute("value") or ""
                    if txt.isdigit():
                        values.append((int(txt), val))
                if len(values) >= 2 and max(v for v, _ in values) >= 20:
                    biggest = max(values)[1]
                    sel.select_option(biggest)
                    page.wait_for_timeout(1200)
                    print(f"  已將每頁筆數調整為 {max(values)[0]}")
                    return
        except Exception:
            continue


def _try_submit(page: Page) -> bool:
    for fr in page.frames:
        for pat in _SUBMIT_PATTERNS:
            for loc in (
                fr.locator(f'input[type="submit"][value*="{pat}"]'),
                fr.get_by_role("button", name=pat, exact=False),
            ):
                try:
                    if loc.count() and loc.first.is_visible():
                        loc.first.click()
                        page.wait_for_timeout(1500)
                        return True
                except Exception:
                    continue
    return False


def _total_pages(page: Page) -> int | None:
    """從頁面「共 N 頁」文字讀出總頁數。"""
    try:
        m = re.search(r"共\s*(\d+)\s*頁", page.inner_text("body"))
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _has_idx_pager(page: Page) -> bool:
    """分頁是否用 ?idx=N 這種 query string 連結（第 2 頁 = idx=1）。"""
    try:
        return page.locator('a[href*="idx="]').count() > 0
    except Exception:
        return False


def _crawl_by_idx(
    page: Page, url: str, total: int, max_pages: int
) -> list[tuple[int, Table]]:
    """以 ?idx=N 逐頁走訪（0-based；首頁即目前畫面，不重新載入）。

    實測發現站方分頁的「下一頁」連結會在最後幾頁提前消失（257 頁的清單
    點到 255 頁就沒了），因此只要頁面有標「共 N 頁」且分頁走 query string，
    就直接按頁數走訪，不依賴「下一頁」。
    """
    collected: list[tuple[int, Table]] = []
    n = min(total, max_pages)
    if total > max_pages:
        print(f"  ⚠️ 總頁數 {total} 超過 max_pages={max_pages}，僅擷取前 {n} 頁。")
    sep = "&" if "?" in url else "?"
    for i in range(n):
        if i:
            page.goto(f"{url}{sep}idx={i}", wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
        tables = extract_tables(page)
        main = largest_table(tables)
        if main is None:
            print(f"  第 {i + 1} 頁：找不到資料表格，略過。")
            continue
        for t in tables:
            if len(t.rows) >= max(3, len(main.rows) // 10):
                collected.append((i + 1, t))
        print(f"  第 {i + 1} 頁：{len(main.rows)} 列（表格 {main.key}）")
    return collected


def _write_csv(out_dir: Path, table_key: str, rows: list[dict], name: str = "") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    # 各頁欄位理論上一致，但保險起見取聯集
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in table_key)[:60]
    prefix = f"{name}_" if name else ""
    path = out_dir / f"fda_{prefix}{safe}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


def fetch(
    page: Page,
    conn,
    url: str = URL,
    max_pages: int = 500,
    out_dir: Path = Path("out"),
    since: str | None = None,
    name: str = "",
) -> dict[str, int]:
    print(f"開啟 {url}" + (f"（回溯至 {since}）" if since else ""))
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass

    initial = largest_table(extract_tables(page))
    if initial is None or len(initial.rows) < 2:
        print("  首頁未見資料表格，嘗試送出查詢…")
        _try_submit(page)

    _maximize_page_size(page)

    stop_when = (lambda t: _page_all_older(t, since)) if since else None

    total = _total_pages(page)
    if total and total > 1 and _has_idx_pager(page):
        print(f"  偵測到「共 {total} 頁」與 ?idx= 分頁連結，改用網址逐頁走訪。")
        pages = _crawl_by_idx(page, url, total, max_pages)
    else:
        pages = crawl_paginated(page, max_pages=max_pages, stop_when=stop_when)
    if not pages:
        raise SystemExit(
            "沒有擷取到任何表格。請改用 `probe` 指令產生頁面結構報告後再調整。"
        )

    by_key: dict[str, list[dict]] = {}
    records: list[dict] = []
    for page_no, t in pages:
        by_key.setdefault(t.key, []).extend(t.rows)
        for rec in t.rows:
            records.append(
                {
                    "row_hash": row_hash(url, t.key, rec),
                    "source_url": url,
                    "table_key": t.key,
                    "page_no": page_no,
                    "data": json.dumps(rec, ensure_ascii=False),
                }
            )

    # 以 row_hash 去重（分頁重疊或表格重複出現時會有）
    dedup = {r["row_hash"]: r for r in records}
    n = db.upsert_fda_rows(conn, dedup.values())

    written = []
    for key, rows in by_key.items():
        seen, uniq = set(), []
        for r in rows:
            h = row_hash(url, key, r)
            if h not in seen:
                seen.add(h)
                uniq.append(r)
        written.append(_write_csv(out_dir, key, uniq, name=name))

    print(f"\n完成：{len(dedup)} 筆不重複資料寫入 SQLite（本次 upsert {n} 次）")
    for p in written:
        print(f"  CSV → {p}")
    return {"rows": len(dedup), "tables": len(by_key)}
