"""通用 HTML 表格擷取與分頁處理。

針對政府網站常見的 ASP.NET WebForms（GridView + __doPostBack 分頁）而寫，
不綁定特定 element id，因此網站小改版時通常仍可運作。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Frame, Page

# 在頁面內把所有表格序列化出來，比逐格 locator 呼叫快上數十倍
_SERIALIZE_TABLES = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('table')).map((t, i) => {
    const rows = Array.from(t.rows).map((r) =>
      Array.from(r.cells).map((c) => {
        const a = c.querySelector('a[href]');
        const cell = { text: clean(c.innerText) };
        if (a && a.href && !a.href.startsWith('javascript:')) cell.href = a.href;
        return cell;
      })
    );
    return {
      index: i,
      id: t.id || null,
      className: t.className || null,
      caption: clean(t.caption ? t.caption.innerText : ''),
      rows,
    };
  });
}
"""

_NEXT_PATTERNS = [
    "下一頁", "下頁", "次頁", "下一筆", "Next", "next", "›", "»", ">>",
]


@dataclass
class Table:
    frame_url: str
    index: int
    id: str | None
    caption: str
    headers: list[str]
    rows: list[dict[str, Any]]

    @property
    def key(self) -> str:
        return self.id or self.caption or f"table[{self.index}]"


def _to_table(frame_url: str, raw: dict[str, Any]) -> Table | None:
    rows = raw.get("rows") or []
    if len(rows) < 2:
        return None

    # 第一列若全為非數字短字串，視為表頭
    head = [c["text"] for c in rows[0]]
    body_start = 1
    if not head or all(not h for h in head):
        head = [f"col{i}" for i in range(len(rows[0]))]
        body_start = 0

    # 表頭去重（政府網站常有重複欄名）
    seen: dict[str, int] = {}
    headers = []
    for h in head:
        h = h or "col"
        if h in seen:
            seen[h] += 1
            h = f"{h}_{seen[h]}"
        else:
            seen[h] = 0
        headers.append(h)

    out: list[dict[str, Any]] = []
    for r in rows[body_start:]:
        if not r or all(not c["text"] for c in r):
            continue
        rec: dict[str, Any] = {}
        for i, cell in enumerate(r):
            name = headers[i] if i < len(headers) else f"col{i}"
            rec[name] = cell["text"]
            if cell.get("href"):
                rec[f"{name}__link"] = cell["href"]
        out.append(rec)

    if not out:
        return None
    return Table(frame_url, raw["index"], raw.get("id"), raw.get("caption", ""), headers, out)


def extract_tables(page: Page) -> list[Table]:
    """抓取主文件與所有 iframe 內的表格。"""
    tables: list[Table] = []
    frames: list[Frame] = list(page.frames)
    for fr in frames:
        try:
            raws = fr.evaluate(_SERIALIZE_TABLES)
        except Exception:
            continue  # 跨網域 iframe 讀不到，略過
        for raw in raws:
            t = _to_table(fr.url, raw)
            if t:
                tables.append(t)
    return tables


def largest_table(tables: list[Table]) -> Table | None:
    return max(tables, key=lambda t: len(t.rows), default=None)


def row_hash(source_url: str, table_key: str, rec: dict[str, Any]) -> str:
    payload = json.dumps(
        {k: v for k, v in sorted(rec.items()) if not k.endswith("__link")},
        ensure_ascii=False,
    )
    return hashlib.sha256(f"{source_url}|{table_key}|{payload}".encode()).hexdigest()[:32]


def _click_next(page: Page) -> bool:
    """找到並點擊「下一頁」。回傳是否成功點擊。"""
    for fr in page.frames:
        for pat in _NEXT_PATTERNS:
            for loc in (
                fr.get_by_role("link", name=pat, exact=False),
                fr.get_by_role("button", name=pat, exact=False),
                fr.locator(f'input[type="submit"][value*="{pat}"]'),
            ):
                try:
                    if loc.count() == 0:
                        continue
                    el = loc.first
                    if not el.is_visible() or not el.is_enabled():
                        continue
                    el.click()
                    return True
                except Exception:
                    continue
    return False


def crawl_paginated(
    page: Page,
    max_pages: int = 200,
    settle_ms: int = 1200,
    stop_when=None,
) -> list[tuple[int, Table]]:
    """逐頁擷取表格，直到沒有下一頁、內容不再變化，或達到 max_pages。

    以「該頁最大表格的內容雜湊」判斷是否真的翻頁成功，避免分頁控制項存在
    但點下去沒反應時陷入無限迴圈。

    stop_when(table) 回傳 True 時提前停止（該頁仍會收錄）——用於日期排序的
    清單「回溯到某日期即止」。
    """
    collected: list[tuple[int, Table]] = []
    seen_pages: set[str] = set()

    for page_no in range(1, max_pages + 1):
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        tables = extract_tables(page)
        main = largest_table(tables)
        if main is None:
            print(f"  第 {page_no} 頁：找不到資料表格，停止。")
            break

        sig = hashlib.sha256(
            json.dumps(main.rows, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        if sig in seen_pages:
            print(f"  第 {page_no} 頁內容與先前重複，判定已到最後一頁。")
            break
        seen_pages.add(sig)

        for t in tables:
            if len(t.rows) >= max(3, len(main.rows) // 10):
                collected.append((page_no, t))
        print(f"  第 {page_no} 頁：{len(main.rows)} 列（表格 {main.key}）")

        if stop_when and stop_when(main):
            print(f"  第 {page_no} 頁達停止條件，結束。")
            break

        if not _click_next(page):
            print(f"  第 {page_no} 頁後沒有「下一頁」，結束。")
            break
        if page_no == max_pages:
            print(
                f"  ⚠️ 已達 max_pages={max_pages} 但仍有「下一頁」，"
                "結果不完整；請加大 --max-pages 重跑。"
            )
        page.wait_for_timeout(settle_ms)

    return collected
