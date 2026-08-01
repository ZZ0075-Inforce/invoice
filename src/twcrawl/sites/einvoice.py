"""財政部電子發票整合服務平台 — 人工登入 + 網路層擷取。

登入一律由人工完成（圖形驗證碼、自然人憑證、行動自然人憑證皆可），
程式只負責保存 session、錄下平台回傳的資料並正規化。

平台為 SPA，資料經由 XHR 回傳，因此擷取網路回應比解析 DOM 穩定得多。
"""

from __future__ import annotations

import base64
import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, NamedTuple

from playwright.sync_api import BrowserContext

from .. import capture_index, db
from ..browser import save_session, wait_for_operator

HOME = "https://www.einvoice.nat.gov.tw/"
SESSION = "einvoice"

INV_NUM_RE = re.compile(r"^[A-Z]{2}\d{8}$")
DATE_RE = re.compile(r"^\d{4}[/-]?\d{2}[/-]?\d{2}$")
# 民國年，如 1130501
ROC_DATE_RE = re.compile(r"^1\d{2}[/-]?\d{2}[/-]?\d{2}$")

# 欄位別名 → 正規化欄位名。平台在不同端點使用不同命名，全部收斂到同一組。
# 2026-07-27 依實際 capture（btc502w/searchCarrierInvoice、common/getCarrierInvoice*、
# 匯出 CSV 表頭）校正。
ALIASES: dict[str, tuple[str, ...]] = {
    "inv_num": ("invnum", "invoicenumber", "invoiceno", "invno", "發票號碼", "發票字軌號碼"),
    "inv_date": ("invdate", "invoicedate", "發票日期", "開立日期", "消費日期"),
    "seller_name": ("sellername", "storename", "店家名稱", "商店名稱", "賣方名稱", "營業人名稱"),
    "seller_ban": ("sellerban", "sellerid", "賣方統編", "統一編號", "營業人統編", "商店統編",
                   "賣方統一編號"),
    "amount": ("amount", "totalamount", "總金額", "金額", "消費金額", "發票金額"),
    "card_type": ("cardtype", "carriertype", "carriername", "載具類別", "載具名稱",
                  "載具自訂名稱"),
    "card_no": ("cardno", "carrierid", "carrierid2", "carrierno", "載具號碼", "載具顯碼"),
    "inv_status": ("invstatus", "invoicestrstatus", "status", "發票狀態", "狀態"),
    "inv_period": ("invperiod", "period", "發票期別", "期別"),
    "donatable": ("invdonatable", "donatable", "donatemark", "可捐贈"),
}
ITEM_ALIASES: dict[str, tuple[str, ...]] = {
    "row_no": ("rownum", "rowno", "seq", "sequencenumber", "序號", "項次"),
    "description": ("description", "itemname", "productname", "item", "品名", "商品名稱",
                    "消費品項", "消費明細品名"),
    "quantity": ("quantity", "qty", "數量", "消費明細數量"),
    "unit_price": ("unitprice", "price", "單價", "消費明細單價"),
    "amount": ("amount", "subtotal", "小計", "金額", "消費明細金額"),
}
ITEM_CONTAINERS = ("details", "invdetails", "items", "detail", "明細", "發票明細")

_LOOKUP = {a: canon for canon, al in ALIASES.items() for a in al}
_ITEM_LOOKUP = {a: canon for canon, al in ITEM_ALIASES.items() for a in al}


# ---------------------------------------------------------------- 互動流程 --

def login(ctx: BrowserContext, session_file: Path) -> None:
    page = ctx.new_page()
    page.goto(HOME, wait_until="domcontentloaded")
    wait_for_operator(
        "請在剛開啟的瀏覽器視窗中完成登入：\n"
        "  1. 選擇「消費者」身分\n"
        "  2. 輸入手機號碼、驗證碼(密碼)與圖形驗證碼（或改用自然人憑證／行動自然人憑證）\n"
        "  3. 確認已進入會員頁面\n"
        "\n本工具不會讀取也不會儲存你的密碼，只保存登入後的 cookie。",
        pump=page,
    )
    p = save_session(ctx, session_file)
    print(f"已保存登入狀態 → {p}（權限 600，已列入 .gitignore）")


def capture(ctx: BrowserContext, cap) -> Path:
    """人工在平台上操作查詢／匯出，工具在背景錄下所有回應。"""
    cap.attach(ctx)
    page = ctx.new_page()
    try:
        page.goto(HOME, wait_until="domcontentloaded")
    except Exception as e:
        # 首頁載入失敗也不要讓視窗關掉——使用者仍可自行輸入網址操作
        print(f"（首頁載入異常：{e}；視窗保持開啟，請自行導向平台）")
    try:
        wait_for_operator(
            "請在瀏覽器中操作（若顯示未登入，請先執行 `login` 或直接在此登入）：\n"
            "  1. 進入「手機條碼專區」→「載具發票查詢」\n"
            "  2. 逐月查詢你要的區間（平台限制起訖須同月）\n"
            "  3. 若有「匯出 CSV」按鈕，請按下匯出（下載檔會一併被收錄）\n"
            "  4. 想抓明細的話，點開幾張發票讓明細 API 被觸發\n"
            "\n過程中所有回傳資料都會被錄到 captures/ 底下。",
            pump=page,  # 等待期間必須持續派發事件，否則什麼都錄不到
        )
    finally:
        # Ctrl+C 或例外中止時也要把索引收尾，已錄到的資料不能作廢
        root = cap.finish()
        print(f"擷取完成 → {root}")
    return root


# ------------------------------------------------------------ 解析 (JSON) --

def _norm_key(k: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(k)).lower()


def _iter_dict_lists(node: Any) -> Iterator[list[dict]]:
    """遞迴走訪 JSON，找出所有「dict 的 list」。"""
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node):
            yield node
        for x in node:
            yield from _iter_dict_lists(x)
    elif isinstance(node, dict):
        for v in node.values():
            yield from _iter_dict_lists(v)


def _map_record(rec: dict, lookup: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in rec.items():
        canon = lookup.get(_norm_key(k))
        if canon and out.get(canon) in (None, ""):
            out[canon] = v
    return out


def _to_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _extract_items(rec: dict, inv_num: str) -> list[dict]:
    items: list[dict] = []
    for k, v in rec.items():
        if _norm_key(k) not in ITEM_CONTAINERS or not isinstance(v, list):
            continue
        for i, raw in enumerate(v, start=1):
            if not isinstance(raw, dict):
                continue
            m = _map_record(raw, _ITEM_LOOKUP)
            items.append(
                {
                    "inv_num": inv_num,
                    "row_no": int(_to_float(m.get("row_no")) or i),
                    "description": m.get("description"),
                    "quantity": _to_float(m.get("quantity")),
                    "unit_price": _to_float(m.get("unit_price")),
                    "amount": _to_float(m.get("amount")),
                    "raw": json.dumps(raw, ensure_ascii=False),
                }
            )
    return items


def parse_json_file(path: Path) -> tuple[list[dict], list[dict]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return [], []

    invoices: list[dict] = []
    items: list[dict] = []
    for lst in _iter_dict_lists(data):
        for raw in lst:
            m = _map_record(raw, _LOOKUP)
            inv_num = str(m.get("inv_num") or "").strip().upper()
            if not INV_NUM_RE.match(inv_num):
                continue  # 不是發票紀錄
            invoices.append(
                {
                    "inv_num": inv_num,
                    "inv_date": _norm_date(m.get("inv_date")),
                    "seller_name": m.get("seller_name"),
                    "seller_ban": m.get("seller_ban"),
                    "amount": _to_float(m.get("amount")),
                    "card_type": m.get("card_type"),
                    "card_no": m.get("card_no"),
                    "inv_status": m.get("inv_status"),
                    "inv_period": m.get("inv_period"),
                    "donatable": m.get("donatable"),
                    "source": f"json:{path.name}",
                    "raw": json.dumps(raw, ensure_ascii=False),
                }
            )
            items.extend(_extract_items(raw, inv_num))
    return invoices, items


def _norm_date(v: Any) -> str | None:
    """統一成 YYYY-MM-DD；民國年自動轉西元、UTC 時間戳轉台北日期。"""
    if not v:
        return None
    raw = str(v).strip()
    if "T" in raw:  # ISO 時間戳（平台以 UTC 儲存，台北日期須 +8 後取日）
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.astimezone(timezone(timedelta(hours=8)))
            return dt.date().isoformat()
        except ValueError:
            pass
    s = re.sub(r"[^\d]", "", raw)
    if len(s) == 8:  # 西元 yyyyMMdd
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) == 7 and s[0] == "1":  # 民國 1130501
        return f"{int(s[0:3]) + 1911}-{s[3:5]}-{s[5:7]}"
    return raw


# ------------------------------------------------------------- 解析 (CSV) --

class CsvParse(NamedTuple):
    """一次 CSV 解析的完整結果。

    `parse_csv_file` 只回 (invoices, items)——`ingest` 掃整個擷取目錄，逐檔
    判讀沒有意義。`import` 相反：使用者指名了這一個檔案，「認不得」與「略過
    了幾列」都必須講出來，靜默丟棄正是這條路最該防的失效（issue #19）。
    """

    invoices: list[dict]
    items: list[dict]
    kind: str          # "header"（新版寬表）／"md"（舊版列型）／"unknown"
    data_rows: int     # 資料列數：扣掉空行，也扣掉表頭那一列
    columns: int       # 第一列的欄數（0＝檔案裡一列都沒有）
    skipped: int       # 認不出發票號碼而略過的列
    # 認得發票號碼、卻沒解出品項的列。**品項欄名對不上時就是這個數字在說話**
    # ——沒有它，`if mi.get("description")` 會把整份明細靜默丟掉，而摘要照樣
    # 印「匯入完成…明細 0 列」以 0 收場（寬表本來就是一列一品項）
    no_item_rows: int


def parse_csv_file(path: Path) -> tuple[list[dict], list[dict]]:
    """解析平台匯出的載具發票 CSV（只要解析結果的呼叫端用這個）。"""
    r = parse_csv_report(path)
    return r.invoices, r.items


def parse_csv_report(path: Path) -> CsvParse:
    """解析平台匯出的載具發票 CSV，連同「認不得／略過」一起回報。

    2026-07 實測的新版匯出（btc502w/downloadInvoiceDetailCSV）是含表頭的
    寬表：一列一品項，發票欄位重複、品項欄位掛 `消費明細_` 前綴。
    偵測到表頭時走表頭對應；否則退回舊的 M/D 混合列型解析
    （以型別特徵定位欄位，不依賴欄序）。原始列一律保留於 raw。
    """
    text = _read_text(path)
    rows = [r for r in csv.reader(io.StringIO(text)) if r and any(c.strip() for c in r)]
    if not rows:
        return CsvParse([], [], "unknown", 0, 0, 0, 0)

    header = [c.strip() for c in rows[0]]
    mapped = [_LOOKUP.get(_norm_key(h)) for h in header]
    is_header = "inv_num" in mapped and not any(
        INV_NUM_RE.match(c.upper()) for c in header
    )
    if is_header:
        inv, items, skipped, no_item = _parse_csv_with_header(
            path, header, rows[1:])
        return CsvParse(inv, items, "header", len(rows) - 1, len(header),
                        skipped, no_item)
    inv, items, skipped = _parse_csv_md(path, rows)
    kind = "md" if inv or items else "unknown"
    # M/D 是一列一角色（M＝表頭、D＝明細），「有發票沒品項」在那裡是正常的
    return CsvParse(inv, items, kind, len(rows), len(header), skipped, 0)


def _parse_csv_with_header(
    path: Path, header: list[str], body: list[list[str]]
) -> tuple[list[dict], list[dict], int, int]:
    invoices: list[dict] = []
    items: list[dict] = []
    seq: dict[str, int] = {}
    skipped = 0
    no_item = 0

    for row in body:
        rec = {header[i]: c.strip() for i, c in enumerate(row) if i < len(header)}
        m = _map_record(rec, _LOOKUP)
        inv_num = str(m.get("inv_num") or "").strip().upper()
        if not INV_NUM_RE.match(inv_num):
            skipped += 1
            continue
        raw = json.dumps(row, ensure_ascii=False)
        invoices.append(
            {
                "inv_num": inv_num,
                "inv_date": _norm_date(m.get("inv_date")),
                "seller_name": m.get("seller_name") or None,
                "seller_ban": m.get("seller_ban") or None,
                "amount": _to_float(m.get("amount")),
                "card_type": m.get("card_type") or None,
                "card_no": m.get("card_no") or None,
                "inv_status": m.get("inv_status") or None,
                "inv_period": None,
                "donatable": m.get("donatable") or None,
                "source": f"csv:{path.name}",
                "raw": raw,
            }
        )
        mi = _map_record(rec, _ITEM_LOOKUP)
        if not mi.get("description"):
            no_item += 1
        else:
            seq[inv_num] = seq.get(inv_num, 0) + 1
            items.append(
                {
                    "inv_num": inv_num,
                    "row_no": int(_to_float(mi.get("row_no")) or seq[inv_num]),
                    "description": mi.get("description"),
                    "quantity": _to_float(mi.get("quantity")),
                    "unit_price": _to_float(mi.get("unit_price")),
                    "amount": _to_float(mi.get("amount")),
                    "raw": raw,
                }
            )
    return invoices, items, skipped, no_item


def _parse_csv_md(
    path: Path, rows: list[list[str]]
) -> tuple[list[dict], list[dict], int]:
    invoices: list[dict] = []
    items: list[dict] = []
    seq: dict[str, int] = {}
    skipped = 0

    for row in rows:
        row = [c.strip() for c in row if c is not None]
        if not row:
            continue
        tag = row[0].upper()
        pos = next((i for i, c in enumerate(row) if INV_NUM_RE.match(c.upper())), None)
        if pos is None:
            skipped += 1
            continue
        inv_num = row[pos].upper()

        rest = [c for i, c in enumerate(row) if i != pos and i > 0]
        date = next(
            (_norm_date(c) for c in rest if DATE_RE.match(c) or ROC_DATE_RE.match(c)), None
        )
        nums = [n for n in (_to_float(c) for c in rest) if n is not None]
        card_no = next((c for c in rest if c.startswith("/")), None)

        if tag == "M":
            # 表頭列格式為 …|商店名稱|發票號碼|…，因此以發票號碼為錨點往前找
            # 第一個文字欄位，即為商店名稱。欄序調整時仍比「取最長字串」可靠。
            invoices.append(
                {
                    "inv_num": inv_num,
                    "inv_date": date,
                    "seller_name": _text_before(row, pos),
                    "seller_ban": next((c for c in rest if re.fullmatch(r"\d{8}", c)), None),
                    "amount": max(nums) if nums else None,
                    "card_type": _text_before(row, row.index(card_no)) if card_no else None,
                    "card_no": card_no,
                    "inv_status": None,
                    "inv_period": None,
                    "donatable": None,
                    "source": f"csv:{path.name}",
                    "raw": json.dumps(row, ensure_ascii=False),
                }
            )
        elif tag == "D":
            # 明細列格式為 …|發票號碼|小計|品名，品名固定在最後
            seq[inv_num] = seq.get(inv_num, 0) + 1
            tail = [c for c in row[pos + 1:] if _is_text(c)]
            items.append(
                {
                    "inv_num": inv_num,
                    "row_no": seq[inv_num],
                    "description": tail[-1] if tail else None,
                    "quantity": None,
                    "unit_price": None,
                    "amount": nums[-1] if nums else None,
                    "raw": json.dumps(row, ensure_ascii=False),
                }
            )
        else:
            # M/D 以外的標籤（頁尾、統計列……）——沒有靜默的餘地，算進略過數
            skipped += 1
    return invoices, items, skipped


def _is_text(c: str) -> bool:
    """非空、非純數值、非日期的欄位才算文字。"""
    return bool(c) and _to_float(c) is None and not (DATE_RE.match(c) or ROC_DATE_RE.match(c))


def _text_before(row: list[str], pos: int) -> str | None:
    """以 pos 為錨點往前找最近的文字欄位。"""
    for c in reversed(row[:pos]):
        if _is_text(c) and not c.startswith("/"):
            return c
    return None


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "big5hkscs", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ------------------------------------------------------------------ 匯入 --

def _jwt_invoice_number(post_data: str) -> str | None:
    """從請求的 JWT 參數解出發票號碼。

    明細端點（common/getCarrierInvoiceDetail）的回應不含發票號碼，
    號碼藏在 POST 的 JWT payload 裡（{"invoiceNumber": "AB12345678", …}）。
    """
    for chunk in re.findall(r"[A-Za-z0-9_-]{16,}", post_data or ""):
        # payload 可能內嵌在較長字串的非 4 對齊位置，四種偏移都試
        for off in range(4):
            sub = chunk[off:]
            pad = sub + "=" * (-len(sub) % 4)
            try:
                blob = base64.urlsafe_b64decode(pad)
            except Exception:
                continue
            m = re.search(rb'"invoiceNumber"\s*:\s*"([A-Z]{2}\d{8})"', blob)
            if m:
                return m.group(1).decode()
    return None


def parse_detail_json(path: Path, inv_num: str) -> list[dict]:
    """解析「單張發票明細」回應（不含發票號碼，由呼叫端提供）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    items: list[dict] = []
    for lst in _iter_dict_lists(data):
        for i, raw in enumerate(lst, start=1):
            m = _map_record(raw, _ITEM_LOOKUP)
            if not m.get("description"):
                continue
            items.append(
                {
                    "inv_num": inv_num,
                    "row_no": int(_to_float(m.get("row_no")) or i),
                    "description": m.get("description"),
                    "quantity": _to_float(m.get("quantity")),
                    "unit_price": _to_float(m.get("unit_price")),
                    "amount": _to_float(m.get("amount")),
                    "raw": json.dumps(raw, ensure_ascii=False),
                }
            )
    return items





def _db_file(conn) -> str:
    """連線實際指向的檔案。以前這裡印的是模組常數，換個資料庫就對不上。"""
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list"):
            if name == "main":
                return file or ":memory:"
    except Exception:
        pass
    return "?"


def merge_invoices(rows: Iterable[dict]) -> list[dict]:
    """同一張發票的多列合併成一筆。

    兩條路都會拿到重複的發票列：CSV 寬表是一列一品項（三個品項就有三列
    同樣的發票），而一次擷取裡同一張發票可能同時來自 CSV 與 JSON，兩邊
    各有對方缺的欄位。所以是**合併而非覆蓋**——空值不蓋掉已有的值。
    """
    merged: dict[str, dict] = {}
    for r in rows:
        cur = merged.setdefault(r["inv_num"], r)
        if cur is not r:
            for k, v in r.items():
                if cur.get(k) in (None, "") and v not in (None, ""):
                    cur[k] = v
    return list(merged.values())


def import_csv(path: Path, conn) -> dict:
    """匯入平台匯出的 CSV（issue #19）。

    平台的查詢區間有下限，早於下限連表頭都拿不到（CLAUDE.md「站台實測
    事實」），所以**這是歷年資料唯一的入口**。也因此「認不得格式」與「略過
    了幾列」一定要講出來：這條路一年跑不到一次，靜默丟棄的話，使用者會以為
    資料已經進去了，而真相要等到看報表少了一整年才會發現。

    只回筆數，不回也不印金額與店家名（那是消費紀錄）。
    """
    if not path.exists():
        raise SystemExit(f"找不到檔案：{path}\n  → 確認路徑，或把檔案拖進終端機取得完整路徑。")
    if path.is_dir():
        raise SystemExit(f"這是一個目錄，不是 CSV 檔案：{path}\n  → 指名要匯入的那個檔案。")

    rep = parse_csv_report(path)
    if rep.columns == 0:
        raise SystemExit(f"這個檔案是空的（{path.name}）\n  → 確認匯出時真的有選到資料。")
    if not rep.invoices and rep.kind == "header":
        # 認得表頭卻一筆都沒解出來：這是「只匯出了表頭」，與「認不得格式」
        # 是兩件事，講錯會讓人跑去追格式問題
        raise SystemExit(
            f"這個 CSV 有認得的表頭，但沒有可用的資料列（{path.name}）\n"
            f"  {rep.data_rows} 個資料列裡沒有一列找得到發票號碼\n"
            "  → 確認匯出時真的有選到資料。"
        )
    if not rep.invoices:
        # 刻意不把讀到的內容印出來——認不得的時候第一列可能根本不是表頭而是
        # 真的消費紀錄，那不該進終端機（比照 db_stats 只印筆數的規矩）
        raise SystemExit(
            f"認不得這個 CSV 的格式（{path.name}）\n"
            f"  讀到 {rep.data_rows} 列、每列 {rep.columns} 欄，"
            "但沒有一列找得到發票號碼（AA12345678 這種形狀）\n"
            "  → 需要的是平台「載具發票明細」匯出的 CSV（含表頭那種），"
            "或舊版 M/D 列型\n"
            "  → 想讓工具支援新格式：提供第一列的欄位名即可（不要任何值）。"
        )

    invoices = merge_invoices(rep.invoices)
    n_inv = db.upsert_invoices(conn, invoices)
    n_item = db.upsert_items(conn, rep.items)
    return {"file": str(path), "kind": rep.kind, "rows": rep.data_rows,
            "skipped": rep.skipped, "no_item_rows": rep.no_item_rows,
            "invoices": n_inv, "items": n_item}


def ingest(capture_dir: Path, conn) -> dict[str, int]:
    invoices: list[dict] = []
    items: list[dict] = []
    index = capture_index.by_file(capture_dir)

    for p in sorted(capture_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() == ".json" and p.name != "index.json":
            i, d = parse_json_file(p)
            if not i and not d:
                # 可能是「單張發票明細」：發票號碼在請求的 JWT 裡
                rel = str(p.relative_to(capture_dir)).replace("\\", "/")
                entry = index.get(rel)
                post = entry.post_data if entry else ""
                inv_num = _jwt_invoice_number(post)
                if inv_num:
                    d = parse_detail_json(p, inv_num)
        elif p.suffix.lower() in (".csv", ".txt", ".bin"):
            i, d = parse_csv_file(p)
        else:
            continue
        if i or d:
            print(f"  {p.name}: 發票 {len(i)} 筆、明細 {len(d)} 列")
        invoices.extend(i)
        items.extend(d)

    n_inv = db.upsert_invoices(conn, merge_invoices(invoices))
    n_item = db.upsert_items(conn, items)
    print(f"\n匯入完成：發票 {n_inv} 筆、明細 {n_item} 列 → {_db_file(conn)}")
    if not n_inv:
        print(
            "沒有辨識出任何發票。請把 captures/ 底下的 index.json 內容提供出來，"
            "即可依實際 API 回應格式補上對應規則。"
        )
    return {"invoices": n_inv, "items": n_item}
