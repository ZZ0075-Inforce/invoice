"""twcrawl csvout — 指令級 CSV 匯出（issue #26）：兩份檔給試算表自己樞紐。

五頁是預先設計好的視角，「某類店家這半年的單價分布」這種臨時問題它答不了，
所以要一份夠細的原始表倒出來。查詢頁的「另存 CSV」不被取代：那是「看得到
的就是要帶走的」，這支是「把全部倒出來自己切」。

**為什麼是兩份而不是一份寬表**：庫內 14.3% 的發票「明細加總 ≠ 發票金額」
（折讓、平台的湊整）。單一寬表不管怎麼設計都會讓某一種加總悄悄變成錯的
——加總品項金額會與儀表板差一截，加總重複填的發票金額則會重複計算。分成
兩份、以發票號碼為鍵，「哪一種加總是對的」才變成使用者選得出來的事；差異
筆數由 `write_csv` 回報，摘要一定要講（不講的話第一次樞紐就會撞到，而那時
沒有任何線索）。

items.csv **冗餘帶日期／店家／分類**：品項層的問題幾乎都要按時間或店家切，
沒這三欄每次都得先 VLOOKUP。重複的是維度不是金額，不造成加總歧義。

資料一律走 `export.invoice_rows`——分類（含品項覆寫）、招牌名、狀態中文與
五頁同一份實作。自己查資料庫組的話，CSV 會跟畫面講不一樣的分類。

載具號碼與 raw 不進 CSV，理由同 ADR-0002 對 data.js 那條紅線。
"""

from __future__ import annotations

import csv
from pathlib import Path

from . import export
from .categories import Classifier
from .workspace import Workspace

# 中文表頭、UTF-8 BOM、CRLF、全欄加引號——與查詢頁的「另存 CSV」同一套慣例。
# 同一個專案兩種 CSV 長不一樣的話，使用者得記哪份是哪個。
INVOICE_HEADER = ["日期", "店家", "統編", "行業", "分類", "非必要",
                  "金額", "發票號碼", "狀態"]
ITEM_HEADER = ["發票號碼", "日期", "店家", "分類", "序號", "品名",
               "數量", "單價", "金額"]

# 明細加總與發票金額差多少才算「對不起來」：金額以元為單位，0.5 以下是
# 浮點與湊整的噪音，不是資料不一致
TOLERANCE = 0.5


def _num(v: float | int | None) -> str:
    """數字寫成人看得懂的樣子。

    **不四捨五入**：單價有小數（62.5），捨掉會讓 CSV 與價格追蹤那頁對不起來。
    整數不留 `.0` 純粹是好看——SQLite 的 REAL 親和性讓 100 存進去是 100.0。
    """
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _open(path: Path):
    """utf-8-sig 寫出 BOM（Excel 雙擊直開不亂碼），newline="" 讓 csv 模組
    自己決定換行，才不會在 Windows 變成 \\r\\r\\n。"""
    return path.open("w", encoding="utf-8-sig", newline="")


def write_csv(conn, ws: Workspace, classifier: Classifier) -> dict:
    """寫出兩份 CSV，回傳筆數與路徑。摘要由 cli 印，這裡不印。"""
    rows, _info = export.invoice_rows(conn, classifier)
    ws.ensure_out()
    n_items = 0
    mismatched = 0
    with _open(ws.csv_invoices) as fi, _open(ws.csv_items) as ft:
        wi = csv.writer(fi, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        wt = csv.writer(ft, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        wi.writerow(INVOICE_HEADER)
        wt.writerow(ITEM_HEADER)
        for r in rows:
            v, si = r.row, r.si
            wi.writerow([v["date"], v["seller"], si.get("ban") or "",
                         si.get("industry") or "", v["category"],
                         "是" if r.cat.unnecessary else "否",
                         _num(v["amount"]), v["num"], v["status"] or ""])
            amounts = []
            # 序號＝該張發票內的第幾列。payload 的 items 不帶資料庫的 row_no，
            # 而讓它帶會動到 data.js；db.invoice_items 本來就依 row_no 排序，
            # 所以這個編號與原始列序一致
            for n, it in enumerate(v["items"], 1):
                wt.writerow([v["num"], v["date"], v["seller"], v["category"],
                             n, it.get("desc") or "", _num(it.get("qty")),
                             _num(it.get("price")), _num(it.get("amount"))])
                n_items += 1
                if it.get("amount") is not None:
                    amounts.append(it["amount"])
            # 沒有明細的發票不算不一致——那是「還沒抓到明細」，不是對不起來
            if amounts and abs(sum(amounts) - v["amount"]) > TOLERANCE:
                mismatched += 1
    return {"invoices": ws.csv_invoices, "items": ws.csv_items,
            "n_invoices": len(rows), "n_items": n_items,
            "mismatched": mismatched}
