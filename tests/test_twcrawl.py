"""端到端測試。

表格擷取與分頁邏輯以本機模擬的 ASP.NET GridView 頁面驗證；
解析器以實際 API／CSV 的資料形狀驗證。

執行（Windows）：python tests\\test_twcrawl.py
執行（macOS / Linux）：python3 tests/test_twcrawl.py

檔案開頭會自行把 src/ 加進 sys.path，因此不需要設定 PYTHONPATH。
"""

from __future__ import annotations

import json
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from twcrawl import db  # noqa: E402
from twcrawl.browser import browser_context  # noqa: E402
from twcrawl.sites import einvoice  # noqa: E402
from twcrawl.tables import crawl_paginated, extract_tables, largest_table  # noqa: E402

PAGES = 3
PER_PAGE = 5


# ------------------------------------------------ 模擬 ASP.NET GridView --

class _Handler(BaseHTTPRequestHandler):
    style = "postback"  # "postback"（GridView 送 form）或 "idx"（FDA 式 ?idx= 分頁）

    def log_message(self, *a):  # 靜音
        pass

    def do_GET(self):
        if self.path.startswith("/api"):
            # 模擬政府網站的壞習慣：內容是 JSON 但 content-type 標 text/plain
            body = json.dumps({"invNum": "ZZ00000000", "amount": "100"}).encode()
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/xhr-delayed"):
            # 頁面載入完成「之後」才發出的 XHR——用來驗證等待迴圈有在派發事件
            body = (b"<!doctype html><html><body>SPA"
                    b"<script>setTimeout(()=>fetch('/api').then(r=>r.text()),1500)"
                    b"</script></body></html>")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/xhr"):
            body = (b"<!doctype html><html><body>SPA"
                    b"<script>fetch('/api').then(r=>r.text())</script></body></html>")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        page = 1
        if "?" in self.path:
            qs = self.path.split("?", 1)[1]
            for kv in qs.split("&"):
                if kv.startswith("p="):
                    page = int(kv[2:])
                elif kv.startswith("idx="):
                    page = int(kv[4:]) + 1  # idx 為 0-based
        self._respond(page)

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length).decode()
        page = 1
        for kv in body.split("&"):
            if kv.startswith("__EVENTARGUMENT="):
                page = int(kv.split("=", 1)[1] or 1)
        self._respond(page)

    def _respond(self, page: int):
        rows = "".join(
            f"<tr><td>{(page - 1) * PER_PAGE + i}</td>"
            f"<td>廠商{(page - 1) * PER_PAGE + i}</td>"
            f"<td>下架品項 {(page - 1) * PER_PAGE + i}</td>"
            f"<td>2024/0{page}/0{i}</td></tr>"
            for i in range(1, PER_PAGE + 1)
        )
        if self.style == "idx":
            # 模擬真實 FDA 站台的怪癖：「下一頁」在最後一頁之前就消失，
            # 但頁面標有「共 N 頁」且分頁連結走 ?idx=（0-based）
            pager = "".join(f'<a href="?idx={n}">{n + 1}</a> ' for n in range(PAGES))
            nxt = (
                f'<a href="?idx={page}">下一頁</a>'
                if page < PAGES - 1
                else "<span>下一頁</span>"
            )
            footer = f"<div>{pager}{nxt} 共 {PAGES} 頁</div>"
        else:
            footer = (
                f'<a href="#" id="next" onclick="document.forms[0].__EVENTARGUMENT.value={page + 1};'
                'document.forms[0].submit();return false;">下一頁</a>'
                if page < PAGES
                else "<span>下一頁</span>"
            )
        html = f"""<!doctype html><html><head><meta charset="utf-8"><title>下架產品清單</title></head>
<body><form method="post"><input type="hidden" name="__EVENTARGUMENT" value="">
<table id="gvList"><tr><th>編號</th><th>業者名稱</th><th>產品名稱</th><th>公告日期</th></tr>
{rows}</table>{footer}</form></body></html>"""
        data = html.encode()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve(style: str = "postback") -> tuple[HTTPServer, str]:
    handler = type("_StyledHandler", (_Handler,), {"style": style})
    srv = HTTPServer(("127.0.0.1", 0), partial(handler))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/"


# ------------------------------------------------------- 資料庫測試資料 --
#
# 發票／品項一列的形狀只定義在 db._INVOICE_KEYS／_ITEM_KEYS——那是 upsert 的
# interface（多的鍵靜靜丟掉、少的存成 NULL），手打字面漂掉不會有人發現：
# 這兩個建構器出現之前，32 個 invoice 字面長出五種鍵組合，有的帶 source／raw
# 有的不帶（兩欄沒有任何讀取端，純噪音）。
#
# 建構器只做兩件事：填四個「每個測試都在講」的欄位、擋下 _INVOICE_KEYS 以外
# 的鍵。第二件才是重點——鍵名打錯（invNum）會被 upsert 丟掉，寫出一列 NULL
# 主鍵的發票。三方一致由 test_db_row_shape_is_pinned 釘住。

def an_invoice(inv_num: str, inv_date: str = "2026-05-01",
               seller_name: str = "測試商行", amount: float = 100.0,
               **extra) -> dict:
    """一列 invoices 測試資料。四個「每個測試都在講」的欄位給位置參數，
    其餘走 **extra（seller_ban／card_no／source／raw…）。

    參數名一律等於欄位名：名字不一致的話，`an_invoice("AA1", seller_name="X")`
    會走進 **extra 再靠 dict 合併順序蓋掉位置參數——結果對，但是靠運氣對。
    """
    row = {"inv_num": inv_num, "inv_date": inv_date,
           "seller_name": seller_name, "amount": amount, **extra}
    _reject_unknown(row, db._INVOICE_KEYS, "an_invoice")
    return row


def an_item(inv_num: str, row_no: int, description: str,
            amount: float = 100.0, **extra) -> dict:
    """一列 invoice_items 測試資料（參數名同樣等於欄位名）。"""
    row = {"inv_num": inv_num, "row_no": row_no, "description": description,
           "amount": amount, **extra}
    _reject_unknown(row, db._ITEM_KEYS, "an_item")
    return row


def _reject_unknown(row: dict, known, who: str) -> None:
    bad = sorted(set(row) - set(known))
    if bad:
        raise AssertionError(
            f"{who}：{bad} 不是資料庫欄位，upsert 會靜默丟掉它們。"
            f"可用的鍵：{sorted(known)}")


# ------------------------------------------------------------- 測試項目 --

def test_table_extraction_and_pagination():
    srv, url = _serve()
    try:
        with browser_context(session_file=None, headed=False) as ctx:
            page = ctx.new_page()
            page.goto(url)

            tables = extract_tables(page)
            main = largest_table(tables)
            assert main is not None, "應找到表格"
            assert main.id == "gvList", f"表格 id 應為 gvList，實得 {main.id}"
            assert main.headers[:2] == ["編號", "業者名稱"], main.headers
            assert len(main.rows) == PER_PAGE, len(main.rows)

            page.goto(url)
            collected = crawl_paginated(page, max_pages=10, settle_ms=200)
            all_rows = [r for _, t in collected for r in t.rows]
            assert len(all_rows) == PAGES * PER_PAGE, f"應收 {PAGES * PER_PAGE} 列，實得 {len(all_rows)}"
            ids = sorted(int(r["編號"]) for r in all_rows)
            assert ids == list(range(1, PAGES * PER_PAGE + 1)), ids
            print(f"✓ 表格擷取與分頁：{PAGES} 頁 / {len(all_rows)} 列")
    finally:
        srv.shutdown()


def test_pagination_terminates_on_repeat():
    """分頁控制項存在但點下去沒反應時，必須停止而非無限迴圈。"""
    class Stuck(_Handler):
        def _respond(self, page: int):  # 永遠回第 1 頁
            super()._respond(1)

    srv = HTTPServer(("127.0.0.1", 0), Stuck)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/"
    try:
        with browser_context(session_file=None, headed=False) as ctx:
            page = ctx.new_page()
            page.goto(url)
            collected = crawl_paginated(page, max_pages=50, settle_ms=100)
            assert len(collected) == 1, f"應只收 1 頁就停止，實得 {len(collected)}"
            print("✓ 分頁重複偵測：正確終止")
    finally:
        srv.shutdown()


def test_fda_fetch_end_to_end():
    """整條 FDA 流程：抓取 → 去重 → 寫入 SQLite → 輸出 CSV。"""
    from twcrawl.sites import fda

    srv, url = _serve()
    try:
        with TemporaryDirectory() as d:
            # Windows 不允許刪除仍被開啟的檔案：連線必須在暫存目錄清理前關閉
            conn = db.connect(Path(d) / "fda.sqlite")
            try:
                with browser_context(session_file=None, headed=False) as ctx:
                    res = fda.fetch(ctx.new_page(), conn, Path(d) / "out",
                                    url=url, max_pages=10)
                assert res["rows"] == PAGES * PER_PAGE, res
                n = conn.execute("SELECT COUNT(*) FROM fda_rows").fetchone()[0]
                assert n == PAGES * PER_PAGE, n

                # 重跑一次不應新增列（僅更新 last_seen）
                with browser_context(session_file=None, headed=False) as ctx:
                    fda.fetch(ctx.new_page(), conn, Path(d) / "out",
                              url=url, max_pages=10)
                n2 = conn.execute("SELECT COUNT(*) FROM fda_rows").fetchone()[0]
                assert n2 == n, f"重跑後應維持 {n} 列，實得 {n2}"

                csvs = list((Path(d) / "out").glob("*.csv"))
                assert csvs, "應輸出 CSV"
                content = csvs[0].read_text(encoding="utf-8-sig")
                assert "業者名稱" in content and "下架品項 15" in content
                row = json.loads(
                    conn.execute("SELECT data FROM fda_rows LIMIT 1").fetchone()[0]
                )
                assert "產品名稱" in row, row
                print(f"✓ FDA 端到端：{n} 列、CSV 已輸出、重跑冪等")
            finally:
                conn.close()
    finally:
        srv.shutdown()


def test_fda_idx_pagination():
    """FDA 式 ?idx= 分頁：「下一頁」提前消失時，仍應依「共 N 頁」補完全部頁面。"""
    from twcrawl.sites import fda

    srv, url = _serve(style="idx")
    try:
        with TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "idx.sqlite")
            try:
                with browser_context(session_file=None, headed=False) as ctx:
                    res = fda.fetch(ctx.new_page(), conn, Path(d) / "out",
                                    url=url, max_pages=10)
                assert res["rows"] == PAGES * PER_PAGE, (
                    f"應收 {PAGES * PER_PAGE} 列（含「下一頁」消失後的頁），實得 {res['rows']}"
                )
                print(f"✓ FDA ?idx= 分頁：{res['rows']} 列，不受「下一頁」提前消失影響")
            finally:
                conn.close()
    finally:
        srv.shutdown()


def test_parse_json_invoice():
    payload = {
        "code": 200,
        "msg": "查詢成功",
        "details": [
            {
                "invNum": "AB12345678",
                "invDate": "2024/05/03",
                "sellerName": "全家便利商店股份有限公司",
                "sellerBan": "22555003",
                "amount": "168",
                "cardType": "3J0002",
                "cardNo": "/ABC.123",
                "invStatus": "已開立",
                "details": [
                    {"rowNum": 1, "description": "御飯糰", "quantity": "2",
                     "unitPrice": "35", "amount": "70"},
                    {"rowNum": 2, "description": "拿鐵咖啡", "quantity": "1",
                     "unitPrice": "98", "amount": "98"},
                ],
            },
            {"someOtherRecord": "不是發票，應被忽略"},
        ],
    }
    with TemporaryDirectory() as d:
        p = Path(d) / "resp.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        inv, items = einvoice.parse_json_file(p)

    assert len(inv) == 1, inv
    r = inv[0]
    assert r["inv_num"] == "AB12345678"
    assert r["inv_date"] == "2024-05-03", r["inv_date"]
    assert r["seller_name"] == "全家便利商店股份有限公司"
    assert r["amount"] == 168.0
    assert len(items) == 2, items
    assert items[0]["description"] == "御飯糰" and items[0]["amount"] == 70.0
    print("✓ JSON 解析：表頭 + 明細")


def test_parse_json_roc_date():
    payload = [{"發票號碼": "CD87654321", "發票日期": "1130501", "店家名稱": "測試商店",
                "總金額": "1,250"}]
    with TemporaryDirectory() as d:
        p = Path(d) / "roc.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        inv, _ = einvoice.parse_json_file(p)
    assert inv[0]["inv_date"] == "2024-05-01", inv[0]["inv_date"]
    assert inv[0]["amount"] == 1250.0
    print("✓ JSON 解析：中文欄名 + 民國年 + 千分位")


def test_parse_csv_header_format():
    """2026-07 新版匯出：含表頭的寬表（一列一品項，品項欄掛「消費明細_」前綴）。"""
    csv_text = (
        "載具自訂名稱,發票日期,發票號碼,發票金額,發票狀態,折讓,賣方統一編號,賣方名稱,"
        "賣方地址,買方統編,消費明細_數量,消費明細_單價,消費明細_金額,消費明細_品名\n"
        "手機條碼,2026/06/15,AB12345678,168,已開立,0,22555003,全家便利商店,台北市,,2,35,70,御飯糰\n"
        "手機條碼,2026/06/15,AB12345678,168,已開立,0,22555003,全家便利商店,台北市,,1,98,98,拿鐵咖啡\n"
        "手機條碼,2026/06/20,CD11112222,50,已開立,0,12345678,測試商行,,,1,50,50,礦泉水\n"
    )
    with TemporaryDirectory() as d:
        p = Path(d) / "export.csv"
        p.write_text(csv_text, encoding="utf-8-sig")
        inv, items = einvoice.parse_csv_file(p)

    nums = {r["inv_num"] for r in inv}
    assert nums == {"AB12345678", "CD11112222"}, nums
    ab = next(r for r in inv if r["inv_num"] == "AB12345678")
    assert ab["inv_date"] == "2026-06-15", ab["inv_date"]
    assert ab["seller_name"] == "全家便利商店"
    assert ab["seller_ban"] == "22555003"
    assert ab["amount"] == 168.0
    assert ab["inv_status"] == "已開立"
    assert len(items) == 3, items
    assert items[0]["description"] == "御飯糰" and items[0]["quantity"] == 2.0
    assert items[1]["row_no"] == 2, items[1]
    print("✓ CSV 解析：新版表頭寬表")


def test_ingest_links_detail_by_jwt():
    """明細回應不含發票號碼——須從 index.json 的 JWT 參數解出並掛回。"""
    import base64

    payload = base64.urlsafe_b64encode(
        json.dumps({"invoiceNumber": "EF55667788", "searchFunc": "x"}).encode()
    ).decode().rstrip("=")
    detail = {
        "content": [
            {"sequenceNumber": "1", "item": "牛奶", "quantity": "1",
             "unitPrice": "88", "amount": "88"},
            {"sequenceNumber": "2", "item": "麵包", "quantity": "2",
             "unitPrice": "30", "amount": "60"},
        ],
        "totalPages": 1,
    }
    with TemporaryDirectory() as d:
        root = Path(d) / "einvoice-20260101-000000"
        (root / "responses").mkdir(parents=True)
        (root / "responses" / "001_getCarrierInvoiceDetail.json").write_text(
            json.dumps(detail, ensure_ascii=False), encoding="utf-8")
        (root / "index.json").write_text(json.dumps([{
            "seq": 1, "kind": "response",
            "url": "https://service-mc.einvoice.nat.gov.tw/btc/cloud/api/common/getCarrierInvoiceDetail",
            "method": "POST", "status": 200, "content_type": "application/json",
            "bytes": 1, "file": "responses\\001_getCarrierInvoiceDetail.json",
            "request_post_data": f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig123456789012345=",
        }], ensure_ascii=False), encoding="utf-8")

        conn = db.connect(Path(d) / "t.sqlite")
        try:
            einvoice.ingest(root, conn)
            rows = conn.execute(
                "SELECT inv_num, row_no, description, amount FROM invoice_items ORDER BY row_no"
            ).fetchall()
            assert len(rows) == 2, rows
            assert rows[0]["inv_num"] == "EF55667788"
            assert rows[0]["description"] == "牛奶" and rows[0]["amount"] == 88.0
            assert rows[1]["row_no"] == 2
            # payload 內嵌在非 4 對齊位置也要解得出來（實際平台就是這樣）
            for prefix in ("x", "xx", "xxx"):
                got = einvoice._jwt_invoice_number(f"eyJhbGciOiJIUzI1NiJ9.{prefix}{payload}.sig")
                assert got == "EF55667788", f"偏移 {len(prefix)} 應仍可解出，實得 {got}"
            # UTC 時間戳應轉為台北日期
            assert einvoice._norm_date("2026-06-29T16:00:00Z") == "2026-06-30"
            print("✓ ingest：明細經 JWT 解碼掛回發票號碼（含非對齊 payload、UTC 轉台北日期）")
        finally:
            conn.close()


def test_parse_csv_md_format():
    csv_text = (
        "M|手機條碼|/ABC.123|2024/05/03|22555003|001|全家便利商店|AB12345678|168|已開立\n"
        "D|手機條碼|/ABC.123|2024/05/03|AB12345678|70|御飯糰\n"
        "D|手機條碼|/ABC.123|2024/05/03|AB12345678|98|拿鐵咖啡\n"
    ).replace("|", ",")
    with TemporaryDirectory() as d:
        p = Path(d) / "export.csv"
        p.write_text(csv_text, encoding="utf-8")
        inv, items = einvoice.parse_csv_file(p)

    assert len(inv) == 1 and inv[0]["inv_num"] == "AB12345678", inv
    assert inv[0]["inv_date"] == "2024-05-03", inv[0]["inv_date"]
    assert inv[0]["seller_ban"] == "22555003", inv[0]["seller_ban"]
    assert inv[0]["card_no"] == "/ABC.123"
    assert len(items) == 2 and items[1]["row_no"] == 2, items
    assert items[0]["description"] == "御飯糰", items[0]
    print("✓ CSV 解析：M/D 混合列型")


def test_netcapture_records_plaintext_xhr():
    """XHR 回應即使 content-type 是 text/plain 也要錄到；索引須隨錄隨寫。"""
    from twcrawl.netcapture import Capture
    from twcrawl.workspace import Workspace

    srv, url = _serve()
    try:
        with TemporaryDirectory() as d:
            ws = Workspace(Path(d))   # 以前得 os.chdir，因為 Capture 寫死相對路徑
            cap = Capture(ws.new_capture("test"))
            assert ws.captures in cap.root.parents, "擷取目錄必須落在工作區內"
            with browser_context(session_file=None, headed=False) as ctx:
                cap.attach(ctx)
                page = ctx.new_page()
                page.goto(url + "xhr")
                page.wait_for_timeout(1500)
            # 未呼叫 finish() 前索引就該存在（隨錄隨寫）
            assert (cap.root / "index.json").exists(), "索引應隨錄隨寫"
            root = cap.finish()
            idx = json.loads((root / "index.json").read_text(encoding="utf-8"))
            hit = [e for e in idx if "/api" in e["url"]]
            assert hit, f"應錄到 /api 的 XHR 回應，實得 {[e['url'] for e in idx]}"
            saved = root / hit[0]["file"]
            assert saved.suffix == ".json", "內容是 JSON 應以 .json 存檔"
            print("✓ netcapture：text/plain 的 XHR 也被錄下、索引隨錄隨寫")
    finally:
        srv.shutdown()


def test_wait_for_operator_pumps_events():
    """等待人工操作期間發生的 XHR 必須被錄到（回歸：卡在 input()/sleep 時事件不派發）。"""
    import os
    import threading

    from twcrawl.browser import wait_for_operator
    from twcrawl.netcapture import Capture
    from twcrawl.workspace import Workspace

    srv, url = _serve()
    try:
        with TemporaryDirectory() as d:
            ws = Workspace(Path(d))
            try:
                flag = Path(d) / "done.flag"
                os.environ["TWCRAWL_DONE_FILE"] = str(flag)
                cap = Capture(ws.new_capture("pump"))
                with browser_context(session_file=None, headed=False) as ctx:
                    cap.attach(ctx)
                    page = ctx.new_page()
                    page.goto(url + "xhr-delayed")  # XHR 在載入完 1.5 秒後才發出
                    threading.Timer(4.0, lambda: flag.write_text("done")).start()
                    wait_for_operator("（測試）", pump=page)
                root = cap.finish()
                idx = json.loads((root / "index.json").read_text(encoding="utf-8"))
                assert any("/api" in e["url"] for e in idx), (
                    f"等待期間的 XHR 應被錄到，實得 {[e['url'] for e in idx]}"
                )
                print("✓ wait_for_operator：等待期間的 XHR 有被錄到（事件持續派發）")
            finally:
                os.environ.pop("TWCRAWL_DONE_FILE", None)
    finally:
        srv.shutdown()


def test_handoff_sanitizes_values():
    """handoff 摘要必須含欄位名稱，且絕不能含任何實際值（發票號碼、店名、條碼、憑證）。"""
    from twcrawl import handoff

    with TemporaryDirectory() as d:
        root = Path(d) / "einvoice-20260101-000000"
        (root / "responses").mkdir(parents=True)
        (root / "responses" / "001_query.json").write_text(json.dumps({
            "code": 200,
            "details": [{
                "invNum": "XY98765432",
                "invDate": "2026/06/15",
                "sellerName": "祕密商店股份有限公司",
                "amount": "1,234",
                "cardNo": "/SECRET1",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        (root / "index.json").write_text(json.dumps([{
            "seq": 1, "kind": "response",
            "url": "https://www.einvoice.nat.gov.tw/api/query?cardNo=%2FSECRET1&token=TOPSECRETTOKEN",
            "method": "POST", "status": 200,
            "content_type": "application/json", "bytes": 999,
            "file": "responses/001_query.json",
            "request_post_data": "cardNo=%2FSECRET1&verify=MYPASSWORD9",
        }, {
            "seq": 2, "kind": "response",
            "url": "https://www.einvoice.nat.gov.tw/api/detail",
            "method": "POST", "status": 200,
            "content_type": "application/json", "bytes": 9,
            "file": "responses/002_none.json",
            # 平台把整個 JWT 當參數「名稱」——名稱也必須被遮蔽
            "request_post_data": "eyJhbGciOiJIUzI1NiJ9.eyJKV1RTRUNSRVRJTlZOVU0iOjF9.c2lnSFVTSDEyMzQ1Njc4OTAxMg=",
        }], ensure_ascii=False), encoding="utf-8")

        text = handoff.summarize(root)

    # 欄位名稱與端點路徑要在
    for must in ("invNum", "invDate", "sellerName", "cardNo", "/api/query", "POST"):
        assert must in text, f"摘要應含 {must}"
    # 任何實際值都不能在
    for secret in ("XY98765432", "祕密商店", "SECRET1", "TOPSECRETTOKEN",
                   "MYPASSWORD9", "2026/06/15", "1,234", "JV1RTRUNSRVRJTlZOVU0"):
        assert secret not in text, f"摘要洩漏了值：{secret}"
    print("✓ handoff 摘要：欄位名保留、所有值已遮蔽")


def test_capture_index_is_backward_compatible():
    """既有 captures/ 目錄必須照樣讀得動，新寫出的檔也要與舊格式相同。

    這是硬約束，不是「最好有」：captures/ 是重新解析的來源，而明細只保存
    近半年——寫出不相容的索引等於把舊擷取變成廢紙，重抓也抓不回來。
    """
    from twcrawl import capture_index

    # 兩個舊寫入端各自產出的形狀，逐字照抄（含下載項缺 method/status、
    # Windows 的反斜線路徑、_Sink 硬寫的 status 200）
    old = [
        {"seq": 1, "kind": "response",
         "url": "https://example.test/api/list?token=SECRET",
         "method": "POST", "status": 200, "content_type": "application/json",
         "bytes": 12, "file": "responses\\001_list.json",
         "request_post_data": '{"page":1}'},
        {"seq": 2, "kind": "download", "url": "https://example.test/x.csv",
         "file": "downloads/x.csv", "bytes": 34},
    ]
    with TemporaryDirectory() as td:
        root = Path(td)
        capture_index.path(root).write_text(
            json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")

        got = capture_index.read_entries(root)
        assert [e.seq for e in got] == [1, 2]
        assert got[0].file == "responses/001_list.json", "反斜線要正規化"
        assert got[0].status == 200 and got[0].post_data == '{"page":1}'
        # 下載項沒有 method/status，不該 KeyError，也不該假裝有值
        assert got[1].kind == "download" and got[1].method == ""
        assert got[1].status is None and got[1].bytes == 34
        assert capture_index.by_file(root)["downloads/x.csv"].url.endswith("x.csv")

        # 壞掉的索引回空清單（呼叫端本來就靠掃目錄取檔，索引是補充）
        capture_index.path(root).write_text("{ 這不是 JSON", encoding="utf-8")
        assert capture_index.read_entries(root) == []
        capture_index.path(root).unlink()
        assert capture_index.read_entries(root) == []

    # 反向：用 Index 寫出來的，要與舊格式逐位元組相同
    with TemporaryDirectory() as td:
        root = Path(td)
        (root / "responses").mkdir()
        (root / "downloads").mkdir()
        idx = capture_index.Index(root)
        assert idx.next_seq == 1, "檔名要帶序號，所以寫檔前得問得到"
        idx.response(url="https://example.test/api/list?token=SECRET",
                     method="POST", status=200,
                     content_type="application/json", size=12,
                     file="responses/001_list.json", post_data='{"page":1}')
        assert idx.next_seq == 2, "加一項就要往前走"
        idx.download(url="https://example.test/x.csv",
                     file=root / "downloads" / "x.csv", size=34)
        written = json.loads(capture_index.path(root).read_text(encoding="utf-8"))
        want = [dict(old[0], file="responses/001_list.json"), old[1]]
        assert written == want, f"寫出的形狀與舊格式不同：\n{written}\n{want}"
    print("✓ 擷取索引：舊目錄讀得動、寫出的形狀與舊格式相同、壞檔不炸")


def test_fetch_sink_records_real_url_and_status():
    """fetch 寫出的索引要帶真實端點與狀態碼，不是檔名字根與硬寫的 200。

    以前 handoff 對 fetch 產物會把 `searchCarrierInvoice_202607_p0` 印在
    「端點」的位置——而那不是因為 _Sink 不知道，真實的 url 與 status 就在
    呼叫點的 scope 裡，只是沒被傳進去。
    """
    from twcrawl import capture_index, handoff
    from twcrawl.sites.einvoice_fetch import _Sink

    with TemporaryDirectory() as td:
        root = Path(td)
        (root / "responses").mkdir()
        sink = _Sink(root)
        sink.add("searchCarrierInvoice_202607_p0", '{"content":[]}',
                 url="https://service-mc.example.test/btc502w/searchCarrierInvoice?page=0",
                 status=200)
        sink.add("getCarrierInvoiceDetail_202607", '{"details":[]}',
                 url="https://service-mc.example.test/common/getCarrierInvoiceDetail",
                 status=500, post="eyJhbGciOiJIUzI1NiJ9.x.y")

        e = capture_index.read_entries(root)
        assert [x.seq for x in e] == [1, 2]
        assert e[0].url.endswith("searchCarrierInvoice?page=0"), e[0].url
        assert e[1].status == 500, "實際狀態碼要留著，不是一律 200"
        assert e[1].post_data.startswith("eyJ"), "明細的 JWT 是發票號碼的來源"
        assert e[0].file == "responses/001_searchCarrierInvoice_202607_p0.json"

        text = handoff.summarize(root)
        assert "service-mc.example.test/btc502w/searchCarrierInvoice" in text, \
            "摘要的端點欄位要是真的端點"
        assert "?page=<str(數值)>" in text, "query 的值仍要遮蔽"
        assert "eyJhbGciOiJIUzI1NiJ9" not in text, "token 不得出現在摘要裡"
    print("✓ fetch 索引：端點與狀態碼是真的、JWT 仍被摘要遮蔽")


def test_fetch_month_range_and_utc_bounds():
    """逐月抓取的月份展開與台北→UTC 邊界（平台的日期參數固定 24 字元）。"""
    from twcrawl.sites import einvoice_fetch as ef

    assert ef._months("2026-03", "2026-06") == [
        (2026, 3), (2026, 4), (2026, 5), (2026, 6)], ef._months("2026-03", "2026-06")
    assert ef._months("2025-11", "2026-02") == [
        (2025, 11), (2025, 12), (2026, 1), (2026, 2)]
    assert ef._months("2026-05", "2026-05") == [(2026, 5)]

    for bad in (("2026-06", "2026-03"), ("2026", "2026-06")):
        try:
            ef._months(*bad)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"{bad} 應該被拒絕")

    # 台北 3/1 00:00 = UTC 2/28 16:00；閏年 2 月要正確
    s, e = ef._utc_range(2026, 3)
    assert s == "2026-02-28T16:00:00.000Z", s
    assert e == "2026-03-31T15:59:59.000Z", e
    assert len(s) == 24 and len(e) == 24, (len(s), len(e))
    s2, _ = ef._utc_range(2024, 3)  # 2024 是閏年
    assert s2 == "2024-02-29T16:00:00.000Z", s2

    # 未來日期會被平台以 HTTP 400 拒絕：當月結束時間須夾到「現在」
    from datetime import datetime as _dt
    now = _dt(2026, 7, 27, 16, 30, 0, tzinfo=ef.TPE)
    s3, e3 = ef._utc_range(2026, 7, now)
    assert s3 == "2026-06-30T16:00:00.000Z", s3
    assert e3 == "2026-07-27T08:30:00.000Z", e3  # 台北 7/27 16:30 = UTC 08:30
    assert len(e3) == 24
    _, e4 = ef._utc_range(2026, 6, now)  # 已過完的月份不受影響
    assert e4 == "2026-06-30T15:59:59.000Z", e4
    print("✓ fetch：月份展開、台北→UTC 邊界（含閏年）、當月夾到現在")


def test_match_invoices_against_fda():
    """比對：店家×業者、品項×產品、品項×警訊標題；純數字、since、
    餐飲現調濾除（菜名撞包裝品名）。"""
    from twcrawl.categories import Classifier
    from twcrawl.match import run_match
    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "m.sqlite")
        try:
            db.upsert_invoices(conn, [
                # 未分類（零售視角）→ 品項參與比對
                an_invoice("AA11111111", "2026-05-01", "測試商行一店", 100.0),
                # since 之前，應排除
                an_invoice("BB22222222", "2026-01-01", "測試商行", 50.0),
                # 餐飲現調 → 濾品項
                an_invoice("CC33333333", "2026-05-02", "巷口快炒小吃店", 120.0),
                # 上榜通路但品項無交集 → 排除
                an_invoice("DD44444444", "2026-05-03", "測試商行二店", 80.0),
                # 上榜通路且無明細 → 保留＋註記
                an_invoice("EE55555555", "2026-05-04", "測試商行三店", 90.0),
            ])
            db.upsert_items(conn, [
                an_item("AA11111111", 1, "特級沙拉油", 100.0),
                an_item("AA11111111", 2, "230", 230.0),           # 純數字
                an_item("AA11111111", 3, "苦茶油", 500.0),
                an_item("CC33333333", 1, "蝦仁蛋炒飯", 120.0),    # 菜名撞即食包品名
                an_item("DD44444444", 1, "抽取式衛生紙", 80.0),   # 與下架清單無交集
            ])
            db.upsert_fda_rows(conn, [
                {"row_hash": "h1", "source_url": "u1", "table_key": "t", "page_no": 1,
                 "data": json.dumps({"業者": "測試商行",
                                     "產品/品項": "特級沙拉油18L"}, ensure_ascii=False)},
                {"row_hash": "h2", "source_url": "u1", "table_key": "t", "page_no": 1,
                 "data": json.dumps({"業者": "某公司", "產品/品項": "泡麵230g"},
                                    ensure_ascii=False)},
                {"row_hash": "h3", "source_url": "u2", "table_key": "n", "page_no": 1,
                 "data": json.dumps({"序號": "1", "標題": "連淨公司苦茶油檢出不合格啟動下架回收",
                                     "發布日期": "2026-07-01"}, ensure_ascii=False)},
                {"row_hash": "h4", "source_url": "u1", "table_key": "t", "page_no": 1,
                 "data": json.dumps({"業者": "台灣卜蜂企業", "產品": "蝦仁蛋炒飯280g"},
                                    ensure_ascii=False)},
            ])
            ws = Workspace(Path(d))
            res = run_match(conn, ws.match_report, Classifier(ws.rules),
                            since="2026-03-01")
            # 店家層級：AA 品項有交集保留、EE 無明細保留；BB 在 since 前、
            # DD 品項皆不在該業者下架清單 → 排除
            assert res["seller_hits"] == 2, res
            assert res["seller_clears"] == 1, res
            assert res["prod_hits"] == 1, res     # 沙拉油命中、230 純數字排除
            assert res["news_hits"] == 1, res     # 苦茶油命中警訊標題
            assert res["eatery_skips"] == 1, res  # 小吃店炒飯×即食包＝濾除
            report = ws.match_report
            assert report.exists(), "應輸出報告 CSV"
            import csv as _csv
            with report.open(encoding="utf-8-sig") as f:
                rows = list(_csv.DictReader(f))
            # 同層級內依發票日期升冪：db.invoices() 保證順序，報告才穩定。
            # 以前不帶 since 時那條 select 沒有 order by，拿到的是 SQLite
            # 的儲存順序（未定義行為），同一份資料兩次匯出可能不同序
            for lvl in ("店家", "品項", "警訊標題"):
                d = [r["inv_date"] for r in rows if r["level"] == lvl]
                assert d == sorted(d), f"{lvl} 的報告列未依日期升冪：{d}"
            prod = next(r for r in rows if r["level"] == "品項")
            assert prod["source"] == "u1", "品項命中應標注來源（食安頁歸戶用）"
            assert not any("蝦仁蛋炒飯" in r["invoice_side"] for r in rows), \
                "餐飲現調的撞名不得進報告"
            assert not any(r["inv_num"] == "DD44444444" for r in rows), \
                "品項皆不在下架清單的上榜通路發票不得進報告"
            ee = next(r for r in rows if r["inv_num"] == "EE55555555")
            assert "無品項明細" in ee["invoice_side"], "無明細保留須註記"
            print("✓ match：三層級命中、餐飲現調撞名濾除、上榜通路品項排除")
        finally:
            conn.close()


def test_db_upsert_is_idempotent():
    with TemporaryDirectory() as d:
        # Windows 不允許刪除仍被開啟的檔案：連線必須在暫存目錄清理前關閉
        conn = db.connect(Path(d) / "t.sqlite")
        try:
            rows = [an_invoice("AB12345678", "2024-05-03", "測試", raw="{}")]
            db.upsert_invoices(conn, rows)
            db.upsert_invoices(conn, rows)
            n = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
            assert n == 1, f"重複匯入應維持 1 筆，實得 {n}"

            items = [an_item("AB12345678", 1, "x")]
            db.upsert_items(conn, items)
            db.upsert_items(conn, items)
            n = conn.execute("SELECT COUNT(*) FROM invoice_items").fetchone()[0]
            assert n == 1, f"明細重複匯入應維持 1 筆，實得 {n}"

            # COALESCE 不變式：後來的部分抓取不得抹掉已知欄位。這條只寫在
            # upsert_invoices 的 SQL 裡，以前沒有測試——而 raw 是刻意的例外。
            # 這裡刻意不用 an_invoice()：建構器會補上日期與店家，正好蓋掉
            # 這個測試要問的事（「只帶號碼」的後續 upsert 會發生什麼）
            db.upsert_invoices(conn, [{"inv_num": "AB12345678"}])
            got = db.invoices(conn)
            assert len(got) == 1, \
                f"只帶號碼的後續 upsert 抹掉了 inv_date（COALESCE 不變式）：{got}"
            assert got[0].seller == "測試" and got[0].amount == 100.0, \
                f"只帶號碼的後續 upsert 不該抹掉店家與金額：{got[0]}"
            # raw 的例外也釘住：它在 SQL 裡沒有 COALESCE，之前只寫在註解裡
            assert conn.execute(
                "select raw from invoices").fetchone()[0] is None, \
                "raw 是刻意的例外：後續 upsert 應以新值（含 NULL）覆蓋"

            b = [{"ban": "12345678", "name": "測試商行",
                  "address": "測試市測試路 1 號", "industry": "餐館", "codes": "56"}]
            db.upsert_biz(conn, b)
            db.upsert_biz(conn, b)
            regs = db.biz_registry(conn)
            assert len(regs) == 1 and regs[0].industry == "餐館", regs
            assert not db.biz_registry(conn, needs_geocode=False)[0].lat
            assert len(db.biz_registry(conn, needs_geocode=True)) == 1, \
                "有地址、沒座標的才進待編碼清單"
            db.set_biz_location(conn, "12345678", 25.0, 121.5)
            assert not db.biz_registry(conn, needs_geocode=True), "編碼後就不該再入列"
            db.upsert_biz(conn, b)
            assert db.biz_registry(conn)[0].lat == 25.0, \
                "重抓對照表不得洗掉已解出的座標"
            print("✓ SQLite upsert 具冪等性、COALESCE 不抹既有值、座標不被對照表覆蓋")
        finally:
            conn.close()


def test_db_row_shape_is_pinned():
    """一列的鍵就是 upsert 的 interface——三方必須一致。

    以前只有 db._INVOICE_KEYS 一份定義，沒有任何斷言：新增欄位卻忘了補進
    常數，那一欄就永遠寫不進去，而且靜悄悄（upsert 讀不到的鍵存成 NULL）。
    """
    with TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "shape.sqlite")
        try:
            for table, keys, build in (
                ("invoices", db._INVOICE_KEYS, db.upsert_invoices),
                ("invoice_items", db._ITEM_KEYS, db.upsert_items),
            ):
                types = {r[1]: r[2] for r in
                         conn.execute(f"PRAGMA table_info({table})")}
                # ① 表欄位 == 常數（fetched_at 由資料庫自填，不由呼叫端給）
                assert set(types) - {"fetched_at"} == set(keys), (
                    f"{table} 的欄位與常數漂了——表多了 "
                    f"{sorted(set(types) - {'fetched_at'} - set(keys))}、"
                    f"常數多了 {sorted(set(keys) - set(types))}")
                # ② 每個鍵都真的寫得進去：全欄位塞值，讀回不該有 NULL。
                #    這條抓的是「常數有這個鍵，但 INSERT 的 SQL 沒綁它」
                row = {k: (1 if types[k] in ("REAL", "INTEGER") else f"<{k}>")
                       for k in keys}
                build(conn, [row])
                got = dict(conn.execute(f"select * from {table}").fetchone())
                blank = sorted(k for k in keys if got[k] is None)
                assert not blank, f"{table} 的 {blank} 沒被寫進去（SQL 漏綁？）"

            # ③ 建構器只吐得出欄位內的鍵，未知鍵當場擋下
            assert set(an_invoice("AA1")) <= set(db._INVOICE_KEYS)
            assert set(an_item("AA1", 1, "x")) <= set(db._ITEM_KEYS)
            # 兩個都是會真的發生的打錯法：payload 那邊叫 price／seller，
            # 從頁面那側複製過來就會寫成這樣
            for bad in (lambda: an_invoice("AA1", seller="測試商行"),
                        lambda: an_item("AA1", 1, "x", price=60.0)):
                try:
                    bad()
                except AssertionError as e:
                    assert "不是資料庫欄位" in str(e), e
                else:
                    raise AssertionError("建構器應擋下不存在的欄位名")
            print("✓ 資料庫列形狀：表欄位、_INVOICE_KEYS／_ITEM_KEYS、建構器三方一致")
        finally:
            conn.close()


def test_db_rejects_null_primary_key():
    """鍵名打錯不得寫出 NULL 主鍵列。

    upsert 只取自己認得的鍵，多的靜靜丟掉——所以 `{"invNum": ...}` 以前會存
    進一列號碼是 NULL 的發票。而 SQLite 的 NULL 不受主鍵唯一性約束：同一列
    upsert 三次就是三列，正好違反本模組「重跑安全」的承諾，且全程無聲。
    （invoice_items 兩個主鍵欄本來就有 NOT NULL，這裡是把 IntegrityError
    換成指得出「你打錯哪個鍵」的訊息。）
    """
    with TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "guard.sqlite")
        try:
            typo = {"invNum": "AB12345678", "inv_date": "2026-05-01",
                    "seller_name": "測試商行", "amount": 100.0}
            for fn, rows in ((db.upsert_invoices, [typo]),
                             (db.upsert_items, [dict(typo, row_no=1)])):
                try:
                    fn(conn, rows)
                except ValueError as e:
                    msg = str(e)
                    assert "inv_num" in msg and "invNum" in msg, \
                        f"訊息要同時說「缺什麼」與「你實際給了什麼」：{msg}"
                    assert "測試商行" not in msg and "100.0" not in msg, \
                        f"訊息只印鍵名，不印值（個資界線）：{msg}"
                else:
                    raise AssertionError(f"{fn.__name__} 應擋下打錯的鍵名")

            # 擋下之後不該留下任何殘骸——以前這裡會多一列 NULL 主鍵的發票
            assert conn.execute("select count(*) from invoices").fetchone()[0] == 0
            assert conn.execute(
                "select count(*) from invoice_items").fetchone()[0] == 0

            # 空字串也算沒有：它進得了 TEXT 主鍵，但一樣不是發票號碼
            for empty in ("", "   "):
                try:
                    db.upsert_invoices(conn, [an_invoice(empty)])
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"空號碼 {empty!r} 應擋下")

            # 既有資料庫才是重點：CREATE TABLE IF NOT EXISTS 不會替既存的表
            # 補上那道 NOT NULL，所以真正保護使用者那份資料庫的是 _require_key。
            # 照舊 schema 先建一次表，讓 db.connect() 跳過建表來驗這條路徑。
            import sqlite3
            old = Path(d) / "old.sqlite"
            pre = sqlite3.connect(old)
            pre.execute(
                "create table invoices (inv_num TEXT PRIMARY KEY, inv_date TEXT,"
                " seller_name TEXT, seller_ban TEXT, amount REAL,"
                " card_type TEXT, card_no TEXT, inv_status TEXT,"
                " inv_period TEXT, donatable TEXT, source TEXT, raw TEXT,"
                " fetched_at TEXT)")
            pre.commit()
            pre.close()
            aged = db.connect(old)
            try:
                notnull = [r[3] for r in aged.execute("PRAGMA table_info(invoices)")
                           if r[1] == "inv_num"]
                assert notnull == [0], \
                    "這個模擬沒生效——舊表應該是「沒有 NOT NULL」的那一種"
                try:
                    db.upsert_invoices(aged, [typo])
                except ValueError:
                    pass
                else:
                    raise AssertionError(
                        "既有資料庫（無 NOT NULL）沒擋下打錯的鍵名——"
                        "使用者那份資料庫正是這一種")
                assert aged.execute(
                    "select count(*) from invoices").fetchone()[0] == 0
            finally:
                aged.close()
            print("✓ 資料庫守衛：打錯鍵名不寫出 NULL 主鍵列（含無 NOT NULL 的既有資料庫）")
        finally:
            conn.close()


def test_categories_two_tier_precedence():
    from twcrawl.categories import Classifier

    with TemporaryDirectory() as td:
        local = Path(td) / "categories.local.json"
        local.write_text(json.dumps({
            "rules": {"全聯": "個人覆寫", "小巷麵館": "餐飲"},
            "unnecessary": ["餐飲"],
        }, ensure_ascii=False), encoding="utf-8")
        cl = Classifier(local_path=local)
        personal = cl.for_seller("全聯實業股份有限公司")
        assert personal.name == "個人覆寫", "個人層應優先於通用層"
        assert personal.source == "personal"
        assert cl.for_seller("小巷麵館").name == "餐飲"
        miss = cl.for_seller("不知名商行")
        assert miss.name == "未分類" and miss.source == "none"
        assert cl.for_seller("小巷麵館").unnecessary \
            and not cl.for_seller("五十嵐測試店").unnecessary, \
            "unnecessary 提供時應整組取代預設"

        default = Classifier(local_path=Path(td) / "沒有這個檔.json")
        cvs = default.for_seller("統一超商股份有限公司")
        assert cvs.name == "便利商店" and cvs.source == "generic"
        assert default.for_seller("五十嵐測試店").name == "手搖飲"
        assert default.for_seller("五十嵐測試店").unnecessary
    print("✓ 店家分類兩層規則（個人優先、未分類後備、source 標明命中層級）")


def test_category_chain_industry_and_item_override():
    """整條優先序鏈都在 Classifier 裡——呼叫端不再自己組。"""
    from twcrawl.categories import Classifier

    with TemporaryDirectory() as td:
        cl = Classifier(local_path=Path(td) / "沒有這個檔.json")
        assert cl.for_seller("神秘小舖").source == "none", "沒接稅籍前應是未分類"

        wired = cl.with_industries({"神秘小舖": "餐盒零售"})
        ind = wired.for_seller("神秘小舖")
        assert ind.name == "餐飲" and ind.source == "industry", \
            "rules 兩層沒中才輪到稅籍行業後備"
        assert ind.eatery
        assert cl.for_seller("神秘小舖").source == "none", \
            "with_industries 回傳新物件，不得汙染原本的快取"

        conv = cl.with_industries({"統一超商股份有限公司": "飲料店"})
        got = conv.for_seller("統一超商股份有限公司")
        assert got.name == "便利商店" and got.source == "generic", \
            "店家規則命中就算數，稅籍不得覆蓋（原本 match 走雙路 OR 會翻掉）"
        assert not got.eatery, "便利商店賣包裝品，不該被當現調濾掉"

        inv = wired.for_invoice("好市多股份有限公司", ["95無鉛汽油"])
        assert inv.name == "加油" and inv.source == "item", "品項覆寫壓在最上層"
        assert wired.for_seller("好市多股份有限公司").name == "量販", \
            "店家業態分類不受品項覆寫影響"
        assert wired.for_invoice("好市多股份有限公司", ["鮮奶"]).name == "量販"
    print("✓ 分類鏈：品項覆寫 → 規則兩層 → 稅籍後備 → 未分類")


def test_industry_primary_first():
    """多重行業以「、」相連、主業在前（bizreg 保留官方順序）——先問主業。"""
    from twcrawl.categories import Classifier

    with TemporaryDirectory() as td:
        cl = Classifier(local_path=Path(td) / "沒有這個檔.json").with_industries({
            "測試甲": "餐館、咖啡館",
            "測試乙": "百貨公司、餐館",
            "測試丙": "咖啡館、餐館",
        })
        assert cl.for_seller("測試甲").name == "餐飲", \
            "主業餐館應勝過次要的咖啡館（整串一起掃會被咖啡館攔截）"
        assert cl.for_seller("測試乙").name == "百貨"
        assert cl.for_seller("測試丙").name == "咖啡", "主業真的是咖啡館就歸咖啡"
    print("✓ 稅籍行業：多重行業以主業優先")


def test_eatery_declaration_merges():
    """自創分類名要宣告才算現調；內建四類是聯集，清不掉。"""
    from twcrawl.categories import Classifier

    with TemporaryDirectory() as td:
        td = Path(td)
        declared = td / "categories.local.json"
        declared.write_text(json.dumps(
            {"rules": {"巷口麵店": "麵食"}, "eatery": ["麵食"]},
            ensure_ascii=False), encoding="utf-8")
        cl = Classifier(local_path=declared)
        assert cl.for_seller("巷口麵店").eatery, "宣告過的自創分類名應算現調"
        assert cl.for_seller("五十嵐測試店").eatery, \
            "eatery 是聯集不是取代——內建四類不會被個人宣告清掉"

        bare = td / "無宣告.json"
        bare.write_text(json.dumps({"rules": {"巷口麵店": "麵食"}},
                                   ensure_ascii=False), encoding="utf-8")
        assert not Classifier(local_path=bare).for_seller("巷口麵店").eatery, \
            "沒宣告就不算——這是補規則時要一起補的那一步"
    print("✓ 餐飲現調：個人分類名可宣告、內建四類清不掉")


def test_export_builds_dashboard():
    from twcrawl import export
    from twcrawl.categories import Classifier
    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as td:
        td = Path(td)
        ws = Workspace(td)
        conn = db.connect(ws.db)
        try:
            db.upsert_invoices(conn, [
                an_invoice("AA1", "2026-05-03", "五十嵐測試店", 60.0),
                an_invoice("AA2", "2026-05-10", "全聯實業股份有限公司", 200.0),
                an_invoice("AA3", "2026-05-12", "神秘小舖", 100.0),
                an_invoice("AA4", "2026-06-01", "全聯實業股份有限公司", 300.0),
            ])
            cl = Classifier(ws.rules)
            payload = export.build_payload(conn, ws, cl)
            assert [m["month"] for m in payload["months"]] == ["2026-05", "2026-06"]
            assert payload["months"][0]["total"] == 360.0
            assert payload["categories"][0]["name"] == "超市", "分類應依金額排序"
            # 非必要判準掛在分類旗標上，payload 不另存一份清單——頁面拿
            # 旗標篩 invoices 就好（以前兩種編碼並存，兩頁各用一種）
            unn = [c for c in payload["categories"] if c["unnecessary"]]
            assert [c["name"] for c in unn] == ["手搖飲"], unn
            assert unn[0]["total"] == 60.0
            assert any(s["name"] == "神秘小舖" for s in payload["uncategorized"])

            dash = export.write_export(conn, ws, cl)
            assert dash.exists()
            data_js = (ws.out / "data.js").read_text(encoding="utf-8")
            assert data_js.startswith("window.TWCRAWL_DATA = ")
        finally:
            conn.close()
    print("✓ export 衍生儀表板資料與模板就位")


def test_catslots_stable_across_exports():
    """色槽指派（issue #10）：資料不變兩次匯出逐槽相同；排名變動不換既有
    分類的色；新進分類取空槽；槽滿時只收回落榜持有者的槽。"""
    from twcrawl import export
    from twcrawl.categories import Classifier
    from twcrawl.workspace import Workspace

    # -- 純函式層：指派規則本身 -------------------------------------------
    a = export.assign_slots({}, ["甲", "乙", "丙"])
    assert a == {"甲": 1, "乙": 2, "丙": 3}, a
    assert export.assign_slots(a, ["丙", "甲", "乙"]) == a, \
        "排名洗牌（成員不變）不得換色"
    b = export.assign_slots(a, ["乙", "丁", "甲", "丙"])
    assert b["丁"] == 4 and all(b[n] == a[n] for n in a), \
        f"新進分類應取最小空槽、不動既有指派：{b}"

    full = {n: i + 1 for i, n in enumerate("甲乙丙丁戊己")}
    got = export.assign_slots(full, list("甲乙丙丁戊庚己"))
    assert got["庚"] == full["己"] and all(got[n] == full[n] for n in "甲乙丙丁戊"), \
        f"槽滿時應收回落榜者（己）的槽給新進者、其餘不動：{got}"
    # 兩個落榜持有者：先收「連 ranked 都不在」的，再收名次最低的
    got = export.assign_slots(full, list("甲乙丙庚辛丁己"))   # 戊消失、己掉到第 7
    assert got["庚"] == full["戊"] and got["辛"] == full["己"], got

    # -- 匯出整合層：持久化在工作區、payload 帶 slot ----------------------
    with TemporaryDirectory() as td:
        ws = Workspace(Path(td))
        conn = db.connect(ws.db)
        try:
            db.upsert_invoices(conn, [
                an_invoice("SL1", "2026-05-03", "全聯實業股份有限公司", 500.0),
                an_invoice("SL2", "2026-05-05", "五十嵐測試店", 60.0),
                an_invoice("SL3", "2026-05-07", "神秘小舖", 100.0),
            ])
            cl = Classifier(ws.rules)
            s1 = {c["name"]: c["slot"]
                  for c in export.build_payload(conn, ws, cl)["categories"]}
            s2 = {c["name"]: c["slot"]
                  for c in export.build_payload(conn, ws, cl)["categories"]}
            assert s1 == s2 == {"超市": 1, "手搖飲": 2, "未分類": None}, \
                f"資料不變，連續兩次匯出的指派應逐槽相同：{s1} vs {s2}"
            assert ws.state_path("catslots").exists(), \
                "指派應持久化在工作區本機狀態（state/catslots.json）"

            # 排名變動：手搖飲超車超市、速食新進——既有不換色、新進取空槽
            db.upsert_invoices(conn, [
                an_invoice("SL4", "2026-06-01", "五十嵐測試店", 600.0),
                an_invoice("SL5", "2026-06-02", "摩斯測試店", 80.0),
            ])
            p3 = export.build_payload(conn, ws, cl)
            s3 = {c["name"]: c["slot"] for c in p3["categories"]}
            assert s3["超市"] == 1 and s3["手搖飲"] == 2, \
                f"排名變動不得換既有分類的色：{s3}"
            assert s3["速食"] == 3, f"新進分類應取未用槽位：{s3}"
            assert p3["categories"][0]["name"] == "手搖飲", \
                "categories 仍依金額排序——順序歸排序、顏色歸槽位"

            # 狀態檔壞掉／手改出違規內容：整份放棄重指派，匯出不得中斷；
            # 「未分類永遠中性灰」不能被手改的狀態檔繞過
            for bad in ("not json",
                        json.dumps({"未分類": 1}, ensure_ascii=False),
                        json.dumps({"超市": True})):
                ws.state_path("catslots").write_text(bad, encoding="utf-8")
                s4 = {c["name"]: c["slot"]
                      for c in export.build_payload(conn, ws, cl)["categories"]}
                assert sorted(v for v in s4.values() if v) == [1, 2, 3] \
                    and s4["未分類"] is None, (bad, s4)
        finally:
            conn.close()  # Windows：先關連線才能清 TemporaryDirectory
    print("✓ 色槽指派：兩次匯出逐槽相同、排名變動不換色、新進取空槽")


def test_item_override_invoice_level():
    from twcrawl import export
    from twcrawl.categories import Classifier
    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as td:
        td = Path(td)
        ws = Workspace(td)
        cl = Classifier(ws.rules)
        cos = "好市多股份有限公司"
        assert cl.for_invoice(cos, ["95無鉛汽油"]).name == "加油"
        assert cl.for_invoice(cos, ["九二無鉛汽油", "礦泉水"]).name == "加油"
        assert cl.for_invoice(cos, ["鮮奶", None, ""]).name == "量販", \
            "沒命中品項規則就落回店家分類"

        local = td / "categories.local.json"
        local.write_text(json.dumps({"item_rules": {"無鉛汽油": "個人品項"}},
                                    ensure_ascii=False), encoding="utf-8")
        assert Classifier(local).for_invoice(
            cos, ["95無鉛汽油"]).name == "個人品項", "品項規則個人層應優先"

        # 接線：發票層級要進 invoices／months，店家層級要進 sellers。
        # 寫反了純測試看不出來，所以這裡留三條斷言。
        conn = db.connect(ws.db)
        try:
            db.upsert_invoices(conn, [
                an_invoice("CS1", "2026-02-18", cos, 1200.0),
                an_invoice("CS2", "2026-02-20", cos, 500.0),
            ])
            db.upsert_items(conn, [
                an_item("CS1", 1, "95無鉛汽油", 1200.0),
                an_item("CS2", 1, "鮮奶", 500.0),
            ])
            payload = export.build_payload(conn, ws, cl)
            by_num = {v["num"]: v for v in payload["invoices"]}
            assert by_num["CS1"]["category"] == "加油", "發票層級應寫進 invoices"
            assert payload["sellers"][0]["category"] == "量販", \
                "店家層級應寫進 sellers"
            assert payload["months"][0]["byCategory"] == \
                {"加油": 1200.0, "量販": 500.0}, "月彙總跟著發票層級走"
        finally:
            conn.close()  # Windows：先關連線才能清 TemporaryDirectory
    print("✓ 品項覆寫：發票層級與店家層級各寫對地方（export 接線）")


def test_backup_roundtrip_and_excludes_state():
    import pyzipper
    from twcrawl import backup as bk
    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as td:
        td = Path(td)
        ws = Workspace(td)
        ws.ensure_out()
        (ws.captures / "a").mkdir(parents=True)
        ws.db.write_bytes(b"sqlite-bytes")
        (ws.captures / "a" / "r.json").write_text("{}", encoding="utf-8")

        out = bk.make_backup("pw123", ws, out_dir=td / "bak")
        with pyzipper.AESZipFile(out) as zf:
            zf.setpassword(b"pw123")
            names = zf.namelist()
            assert any(n.endswith("twcrawl.sqlite") for n in names)
            got = zf.read(next(n for n in names if n.endswith("r.json")))
            assert got == b"{}", "解密內容應與原檔一致"
            # 包內路徑相對工作區 root，不隨執行目錄變動
            assert "captures/a/r.json" in names, names

        wrong_ok = False
        try:
            with pyzipper.AESZipFile(out) as zf:
                zf.setpassword(b"wrong")
                zf.read(names[0])
            wrong_ok = True
        except Exception:
            pass
        assert not wrong_ok, "錯誤密碼不應能解密"

        # ADR-0001 紅線：state/ 是登入 cookie，永遠不進備份包。
        # 以前這個測試從沒建立過 state/，所以底下兩道防線都沒被驗證過。
        # 防線一：state/ 本來就不在收集範圍。
        ws.ensure_state()
        (ws.state / "einvoice.json").write_text("{}", encoding="utf-8")
        out2 = bk.make_backup("pw123", ws, out_dir=td / "bak2")
        with pyzipper.AESZipFile(out2) as zf:
            assert all("state" not in n.split("/") for n in zf.namelist()), \
                f"備份包不得含 state/：{zf.namelist()}"

        # 防線二：萬一 state 檔混進了收集範圍，backup.py 的 assert 要當場擋下。
        (ws.captures / "state").mkdir()
        (ws.captures / "state" / "leak.json").write_text("{}", encoding="utf-8")
        blocked = False
        try:
            bk.make_backup("pw123", ws, out_dir=td / "bak3")
        except AssertionError as e:
            blocked = "備份絕不收 state/" in str(e)
        assert blocked, "state 檔混入收集範圍時必須當場失敗"
    print("✓ backup 加密可往返、錯誤密碼被拒、state/ 兩道防線都成立")


def test_restore_roundtrip_refuses_clobber_and_state():
    """備份 → 還原往返：筆數一致、既有資料不被默默蓋掉、state/ 不會憑空出現。

    這條路一年跑不到一次，壞掉的代價卻是全部資料，所以六種失敗（工作區已有
    資料、密碼錯、檔案不在、不是備份包、包內路徑越界、包裡有 state/）都在這裡
    釘住——而且每一種都要「一個檔案都不寫出去」。
    """
    import pyzipper
    from twcrawl import backup as bk
    from twcrawl import db as db_mod
    from twcrawl.commands import cmd_restore, db_stats
    from twcrawl.workspace import Workspace

    def fails_with(fn, needle: str) -> None:
        try:
            fn()
        except SystemExit as e:
            assert needle in str(e), f"訊息要講人話且提到「{needle}」：{e}"
            return
        raise AssertionError(f"應該要失敗並提到「{needle}」")

    with TemporaryDirectory() as td:
        td = Path(td)
        src = Workspace(td / "src")
        src.ensure_out()
        conn = db_mod.connect(src.db)
        try:
            db_mod.upsert_invoices(conn, [an_invoice("AA00000001", "2026-05-01"),
                                          an_invoice("AA00000002", "2026-06-02")])
            db_mod.upsert_items(conn, [an_item("AA00000001", 1, "測試品項")])
        finally:
            conn.close()  # Windows：先關連線才能備份／清理
        (src.captures / "einvoice-x" / "responses").mkdir(parents=True)
        (src.captures / "einvoice-x" / "responses" / "r.json").write_text(
            "{}", encoding="utf-8")
        # 手工累積的個人設定：少了它們的還原是靜默降級（畫面照出，只是
        # 店家全掉回通用規則），所以備份包收、還原也要放回來
        src.rules.write_text('{"rules": {"小巷麵館": "餐飲"}}', encoding="utf-8")
        src.budget.write_text('{"monthly": 25000}', encoding="utf-8")
        src.ensure_state()
        (src.state / "einvoice.json").write_text("{}", encoding="utf-8")

        pack = bk.make_backup("pw123", src, out_dir=td / "packs")
        before = db_stats(src)
        assert (before.invoices, before.items, before.last) == \
            (2, 1, "2026-06-02"), before

        # -- 換機：空目錄還原 -------------------------------------------------
        dst = Workspace(td / "dst")
        dst.root.mkdir()
        res = cmd_restore(dst, pack, "pw123")
        assert res["verify"] == before, f"還原後筆數要一致：{res['verify']}"
        assert (dst.captures / "einvoice-x" / "responses" / "r.json"
                ).read_text(encoding="utf-8") == "{}", "captures/ 也要回來"
        assert "小巷麵館" in dst.rules.read_text(encoding="utf-8"), \
            "個人分類規則要跟著回來，否則換機是靜默降級"
        assert dst.budget.exists(), "預算設定也在包裡"
        assert not dst.state.exists(), "還原不該生出 state/（登入 cookie）"

        # -- 已經有資料就不默默覆蓋 -------------------------------------------
        # 覆蓋偵測的範圍必須由 _targets() 導出。手打第二份清單的話，備份日後
        # 多收一個東西這裡會靜默漏掉——而這條分支漏掉就是默默蓋掉使用者資料
        assert bk.existing_data(dst) == [p for p in bk._targets(dst)
                                         if p.exists()], \
            "existing_data 要涵蓋 _targets 裡每一個真的存在的東西"
        fails_with(lambda: cmd_restore(dst, pack, "pw123"), "已經有資料")
        forced = cmd_restore(dst, pack, "pw123", force=True)
        assert forced["verify"] == before, "--force 要真的還原"

        # -- 壞掉的輸入都給人話，而且一個檔案都不寫出去 -----------------------
        fresh = Workspace(td / "fresh")
        fresh.root.mkdir()
        fails_with(lambda: cmd_restore(fresh, pack, "wrong"), "密碼錯誤")
        assert not fresh.db.exists(), "密碼錯誤時不該留下半套還原"
        fails_with(lambda: cmd_restore(fresh, td / "nope.zip", "pw123"),
                   "找不到備份包")

        plain = td / "plain.zip"
        with pyzipper.AESZipFile(plain, "w") as zf:
            zf.writestr("readme.txt", "不是備份包")
        fails_with(lambda: cmd_restore(fresh, plain, "pw123"),
                   "不是 twcrawl 備份包")

        # 資料庫還不存在時 backup 也會產出一個只有個人設定的**合法**包，
        # 所以「沒有資料庫」不可以說成「不是 twcrawl 備份包」——那句話是假的
        nodb = td / "nodb.zip"
        with pyzipper.AESZipFile(nodb, "w") as zf:
            zf.writestr("budget.local.json", "{}")
        fails_with(lambda: cmd_restore(fresh, nodb, "pw123"), "沒有資料庫")

        # 包內路徑是不可信輸入：`..` 不能把檔案寫到工作區外
        slip = td / "slip.zip"
        with pyzipper.AESZipFile(slip, "w") as zf:
            zf.writestr("out/twcrawl.sqlite", b"x")
            zf.writestr("../evil.txt", "x")
        fails_with(lambda: cmd_restore(fresh, slip, "pw123"), "工作區外")
        assert not (td / "evil.txt").exists(), "越界的項目不該真的被寫出來"

        # ADR-0001 紅線在還原端也守：手工塞了 state/ 的包整份拒絕
        tainted = td / "tainted.zip"
        with pyzipper.AESZipFile(tainted, "w") as zf:
            zf.writestr("out/twcrawl.sqlite", b"x")
            zf.writestr("state/einvoice.json", "{}")
        # 訊息要指名紅線本身，不是只是把路徑照抄一遍——照抄的話，把 state 的
        # 判斷整條拿掉、讓它掉到「不是備份包會有的內容」也一樣過得了關
        fails_with(lambda: cmd_restore(fresh, tainted, "pw123"),
                   "登入 cookie 永不備份")
        assert not fresh.state.exists() and not fresh.db.exists(), \
            "整份拒絕就不該留下任何東西"
    print("✓ restore 往返筆數一致；既有資料／密碼／檔案／包形狀／越界／state 都擋")


def test_bizreg_filters_needed_bans():
    import zipfile

    from twcrawl import bizreg

    header = ("營業地址,統一編號,總機構統一編號,營業人名稱,資本額,設立日期,"
              "組織別名稱,使用統一發票,行業代號,名稱,行業代號1,名稱1")
    rows = [
        "台中市測試區測試路1號,12345678,,拾光咖啡有限公司,100000,1120101,有限公司,Y,563111,咖啡館,562011,餐盒零售",
        "台北市測試街2號,87654321,,無關商行,50000,1120101,獨資,Y,451111,布疋批發,,",
    ]
    with TemporaryDirectory() as td:
        td = Path(td)
        zpath = td / "BGMOPEN1.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("BGMOPEN1.csv", "﻿" + header + "\n" + "\n".join(rows))
        conn = db.connect(td / "t.sqlite")
        try:
            db.upsert_invoices(conn, [
                an_invoice("BB1", "2026-05-01", "拾光咖啡有限公司",
                           seller_ban="12345678"),
            ])
            n = bizreg.refresh(conn, cache=zpath)
            assert n == 1, f"應只留資料庫出現過的統編，實得 {n}"
            row = conn.execute(
                "select name, address, industry from biz_registry "
                "where ban='12345678'").fetchone()
            assert row["name"] == "拾光咖啡有限公司"
            assert row["address"] == "台中市測試區測試路1號"
            assert row["industry"] == "咖啡館、餐盒零售"
            none = conn.execute(
                "select count(*) from biz_registry where ban='87654321'"
            ).fetchone()[0]
            assert none == 0, "無關統編不應入庫"
        finally:
            conn.close()
    print("✓ bizreg：欄位關鍵字定位、只留自己的統編")


def test_export_industry_fallback_and_alias():
    from twcrawl import export
    from twcrawl.categories import Classifier
    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as td:
        td = Path(td)
        local = td / "categories.local.json"
        local.write_text(json.dumps(
            {"aliases": {"神秘": "MYSTERY LAB"}}, ensure_ascii=False),
            encoding="utf-8")
        conn = db.connect(td / "t.sqlite")
        try:
            db.upsert_invoices(conn, [
                an_invoice("CC1", "2026-05-01", "神秘小舖",
                           seller_ban="12345678"),
            ])
            conn.execute(
                "insert into biz_registry (ban, name, address, industry) "
                "values ('12345678','神秘小舖','台中市測試路1號','餐盒零售')")
            conn.commit()
            payload = export.build_payload(conn, Workspace(td), Classifier(local))
            s = payload["sellers"][0]
            # 接線：build_payload 必須自己把稅籍行業接上 Classifier，
            # 不能指望呼叫端建構時就傳（serve 就是在拿到 conn 之前建的）
            assert s["category"] == "餐飲", "export 應把稅籍行業接進分類鏈"
            assert s["industry"] == "餐盒零售"
            assert s["name"] == "MYSTERY LAB" and s["legal"] == "神秘小舖"
            assert s["address"] == "台中市測試路1號"
            assert payload["uncategorized"] == [], "行業後備成功就不算未分類"
        finally:
            conn.close()
    print("✓ export：稅籍行業接線、招牌名別名")


def test_geocode_address_cleanup():
    from twcrawl.geocode import _clean, _road_level, _strip_village

    a = _clean("臺中市測試區平安里５鄰示範路３段１２３巷４５號６樓")
    assert a == "臺中市測試區平安里5鄰示範路3段123巷45號", a
    b = _strip_village(a)
    assert b == "臺中市測試區示範路3段123巷45號", b
    assert _road_level(b) == "臺中市測試區示範路3段123巷"
    keep = _strip_village(_clean("南投縣埔里鎮示範路一段99號"))
    assert keep == "南投縣埔里鎮示範路一段99號", "地名裡的「里」不能被當里名刪掉"
    print("✓ geocode：稅籍地址清洗（全形、里鄰、降級到路段）")


def test_update_auto_month_range():
    import datetime as dtm

    from twcrawl.commands import auto_month_range

    assert auto_month_range("2026-06-15", dtm.date(2026, 7, 27)) == ("2026-06", "2026-07")
    assert auto_month_range(None, dtm.date(2026, 2, 10)) == ("2025-09", "2026-02")
    print("✓ update 自動月份區間（重抓最新月補漏、空庫回推 5 個月）")


def test_run_steps_records_and_continues():
    """一步失敗不該拖死後面的步驟；Ctrl+C 則要停掉整輪。"""
    from twcrawl.commands import Step, run_steps

    seen: list[str] = []

    def ok(name):
        def _run():
            seen.append(name)
            return {"who": name}
        return _run

    def boom():
        seen.append("boom")
        # SystemExit 是這個 codebase 的主要錯誤通道（fda、einvoice_fetch、
        # backup…），`except Exception` 攔不到——runner 必須攔得住
        raise SystemExit("來源掛了")

    summary = run_steps([
        Step("a", ok("a")),
        Step("b", boom),
        Step("c", ok("c")),
        Step("d", skip_reason="--no-d"),
    ])

    assert seen == ["a", "boom", "c"], f"失敗的下一步必須照跑：{seen}"
    assert summary["total"] == 4, "跳過的步驟仍佔一個編號"
    assert summary["results"]["a"] == {"who": "a"}
    assert "b" not in summary["results"], "失敗的步驟不該留下結果"
    assert [f.label for f in summary["failed"]] == ["b"]
    assert summary["failed"][0].detail == "來源掛了"
    assert [s.label for s in summary["skipped"]] == ["d"]
    assert summary["skipped"][0].detail == "--no-d"

    def interrupt():
        raise KeyboardInterrupt

    try:
        run_steps([Step("x", interrupt), Step("y", ok("y"))])
        raise AssertionError("KeyboardInterrupt 應該中止整輪")
    except KeyboardInterrupt:
        pass
    assert "y" not in seen, "人工中止後不該再跑下一步"
    print("✓ update 步驟 runner（失敗續跑、跳過佔號、Ctrl+C 中止整輪）")


def test_update_steps_assembly():
    """旗標怎麼對應到七步：區間與回溯日期進標籤，跳過的仍佔編號。"""
    import datetime as dtm

    from twcrawl.commands import update_steps
    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as d:
        ws = Workspace(Path(d))
        conn = db.connect(ws.db)
        try:
            db.upsert_invoices(conn, [an_invoice("AA1", "2026-06-03")])
            today = dtm.date(2026, 7, 30)

            full = update_steps(conn, ws, password="pw", today=today)
            assert len(full) == 7, "七步"
            assert full[1].label == "fetch 2026-06 ～ 2026-07", full[1].label
            # FDA 回溯 90 天，且只對 feed 型來源有意義
            assert "2026-05-01" in full[2].label, full[2].label
            assert not any(s.skip_reason for s in full), "全開時不該有跳過"

            partial_ = update_steps(conn, ws, login=False,
                                    backup=False, today=today)
            assert len(partial_) == 7, "跳過的步驟仍佔編號"
            assert partial_[0].skip_reason == "--no-login"
            assert partial_[-1].skip_reason == "--no-backup"

            nopw = update_steps(conn, ws, password=None, today=today)
            assert "TWCRAWL_BACKUP_PASSWORD" in nopw[-1].skip_reason
            assert nopw[-1].run is None, "沒密碼就不該有可執行的備份步驟"
        finally:
            conn.close()  # Windows：先關連線才能清 TemporaryDirectory
    print("✓ update 步驟組裝（區間與回溯日期入標籤、旗標對應跳過原因）")


def test_latest_capture_prefers_newest_not_alphabetical():
    """capture 目錄與 fetch 目錄混在一起時，要按時間取而不是按檔名。"""
    import os as _os

    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as d:
        ws = Workspace(Path(d))
        caps = ws.captures
        older_fetch = caps / "einvoice-fetch-20260101-000000"
        newer_capture = caps / "einvoice-20260730-120000"
        for p in (older_fetch, newer_capture):
            p.mkdir(parents=True)
        _os.utime(older_fetch, (1_000_000, 1_000_000))
        _os.utime(newer_capture, (2_000_000, 2_000_000))

        assert sorted(p.name for p in caps.iterdir())[-1] == older_fetch.name, \
            "前提：字典序下 einvoice-f 確實會贏過 einvoice-2"
        assert ws.latest_capture().name == newer_capture.name, \
            "應按 mtime 取最新，不是字典序"
    print("✓ 最新擷取目錄按 mtime 取（fetch 目錄不會恆勝人工 capture）")


def test_cmd_handoff_writes_summary():
    """handoff 指令的寫檔行為（原本鎖在 cli.main 裡，測不到）。"""
    from twcrawl.commands import cmd_handoff
    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as d:
        ws = Workspace(Path(d))
        root = ws.captures / "einvoice-20260730-090000"
        (root / "responses").mkdir(parents=True)
        (root / "responses" / "001_query.json").write_text(json.dumps({
            "details": [{"invNum": "XY98765432", "sellerName": "祕密商店"}],
        }, ensure_ascii=False), encoding="utf-8")
        (root / "index.json").write_text(json.dumps([{
            "seq": 1, "kind": "response",
            "url": "https://example.test/api/query?token=TOPSECRETTOKEN",
            "method": "POST", "status": 200,
            "content_type": "application/json", "bytes": 99,
            "file": "responses/001_query.json",
        }], ensure_ascii=False), encoding="utf-8")

        out = ws.out
        res = cmd_handoff(ws, capture_dir=root)

        written = Path(res["path"])
        assert written.parent == out, "應寫進指定的 out_dir"
        assert written.name == f"handoff_{root.name}.txt"
        text = written.read_text(encoding="utf-8")
        assert text == res["text"], "回傳的文字與寫出的檔案必須一致"
        assert "invNum" in text, "欄位名要留著"
        for secret in ("XY98765432", "祕密商店", "TOPSECRETTOKEN"):
            assert secret not in text, f"摘要洩漏了值：{secret}"
    print("✓ handoff 指令：摘要同步寫檔、內容去值化")


def test_backup_password_sources():
    """環境變數優先；非互動終端機回 None 而不是卡在提示上。"""
    import os as _os

    from twcrawl.commands import backup_password

    saved_env = _os.environ.pop("TWCRAWL_BACKUP_PASSWORD", None)
    saved_stdin = sys.stdin
    try:
        _os.environ["TWCRAWL_BACKUP_PASSWORD"] = "來自環境變數"
        assert backup_password() == "來自環境變數"

        del _os.environ["TWCRAWL_BACKUP_PASSWORD"]
        sys.stdin = None  # 模擬非互動終端機；絕不能走到 getpass
        assert backup_password() is None
    finally:
        sys.stdin = saved_stdin
        _os.environ.pop("TWCRAWL_BACKUP_PASSWORD", None)
        if saved_env is not None:
            _os.environ["TWCRAWL_BACKUP_PASSWORD"] = saved_env
    print("✓ 備份密碼來源（環境變數優先、非互動回 None）")


def test_export_items_and_query_page():
    from twcrawl import export
    from twcrawl.categories import Classifier
    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as td:
        td = Path(td)
        ws = Workspace(td)
        conn = db.connect(ws.db)
        try:
            db.upsert_invoices(conn, [
                an_invoice("DD1", "2026-05-03", "五十嵐測試店", 60.0,
                           card_no="/SECRET99", inv_status="INVOICE0003S"),
                an_invoice("DD2", "2026-05-04", "五十嵐測試店", 55.0,
                           inv_status="INVOICE0042X"),
            ])
            db.upsert_items(conn, [
                an_item("DD1", 1, "珍珠鮮奶茶", 60.0, quantity=1,
                        unit_price=60.0),
            ])
            cl = Classifier(ws.rules)
            payload = export.build_payload(conn, ws, cl)
            row = payload["invoices"][0]
            assert row["num"] == "DD1", "發票列應帶發票號碼（查詢頁對帳用）"
            assert row["items"][0]["desc"] == "珍珠鮮奶茶"
            assert row["items"][0]["price"] == 60.0
            # 狀態翻中文（issue #14）：已收錄→中文；未收錄→原始碼不吞資訊；
            # 常態與否由匯出端旗標（頁面不解讀譯文）
            assert row["status"] == "開立" and not row["statusFlagged"], row
            assert payload["invoices"][1]["status"] == "INVOICE0042X", \
                "未收錄的狀態碼要原樣進 payload，不是 None 也不是空字串"
            assert payload["invoices"][1]["statusFlagged"], \
                "未收錄碼是非常態，statusFlagged 要為真（列上才會標）"
            assert "SECRET99" not in json.dumps(payload, ensure_ascii=False), \
                "載具號碼永不進 data.js（ADR-0002）"

            export.write_export(conn, ws, cl)
            assert (ws.out / "query.html").exists(), "export 應就位查詢頁"
            assert (ws.out / "fda.html").exists(), "export 應就位食安頁"
            # ui.js／ui.css 是五頁的必要相依：漏掉的話五頁都壞，而且壞成
            # 「看起來像沒資料」。write_export 複製整個 web/ 就是為了這個
            for asset in ("ui.js", "ui.css", "vendor/leaflet.js"):
                assert (ws.out / asset).exists(), f"export 應就位 {asset}"
        finally:
            conn.close()
    print("✓ export：品項與發票號碼進 data.js、查詢頁就位、載具號碼排除")


def test_serve_rules_writeback():
    import threading
    import urllib.request

    from twcrawl import export
    from twcrawl import serve as serve_mod
    from twcrawl.categories import Classifier
    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as td:
        td = Path(td)
        ws = Workspace(td)
        local, out = ws.rules, ws.out
        conn = db.connect(ws.db)
        try:
            db.upsert_invoices(conn, [
                an_invoice("EE1", "2026-05-01", "神祕測試店"),
            ])
            local.write_text(json.dumps(
                {"aliases": {"神祕": "MYSTERY"},
                 "item_rules": {"無鉛汽油": "加油"}},
                ensure_ascii=False), encoding="utf-8")
            export.write_export(conn, ws, Classifier(local))
        finally:
            conn.close()

        httpd = serve_mod.make_server(ws, port=0)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            port = httpd.server_address[1]
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/rules",
                data=json.dumps({"set": {"神祕測試": "餐飲"}}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as resp:
                j = json.loads(resp.read().decode("utf-8"))
            assert j["ok"] and j["count"] == 1
            cfg = json.loads(local.read_text(encoding="utf-8"))
            assert cfg["rules"]["神祕測試"] == "餐飲"
            assert cfg["aliases"] == {"神祕": "MYSTERY"}, "其他欄位要保留"
            assert cfg["item_rules"] == {"無鉛汽油": "加油"}, "品項規則要保留"
            data_js = (out / "data.js").read_text(encoding="utf-8")
            assert "餐飲" in data_js, "寫回後應重生 data.js 讓分類生效"
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/dashboard.html") as resp:
                assert resp.status == 200, "靜態頁面也要能服務"
            # 招牌名（aliases）寫回：可單獨存，且不動既有規則與別名
            req2 = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/rules",
                data=json.dumps({"aliases": {"神祕測試": "神祕小館"}}
                                ).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req2) as resp:
                assert json.loads(resp.read().decode("utf-8"))["ok"]
            cfg2 = json.loads(local.read_text(encoding="utf-8"))
            assert cfg2["aliases"]["神祕測試"] == "神祕小館", "招牌名要併入"
            assert cfg2["aliases"]["神祕"] == "MYSTERY", "既有別名要保留"
            assert cfg2["rules"]["神祕測試"] == "餐飲", "規則不受招牌名寫回影響"
        finally:
            httpd.shutdown()
            httpd.server_close()
    print("✓ serve：/api/rules 寫回規則＋招牌名（保留其他欄位）並重生 data.js")


def _post_json(port: int, path: str, payload: dict, origin: str | None = None):
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_launcher_explains_missing_venv():
    """一鍵啟動器（#18、#21 改為開控制台）：不在工作區時給人話並回非零，而不是閃退。

    順帶釘住編碼這件事：中文一旦被搬回 .bat，cmd 會拿主控台的 OEM codepage
    （繁中 Windows 是 cp950）逐位元組解析 UTF-8，指令列被切成假指令、結束碼
    還會錯回 0（實測過）。所以主體在 .ps1、.bat 保持純 ASCII——搬回去的話
    這支測試會變紅。
    """
    import shutil
    import subprocess

    root = Path(__file__).resolve().parent.parent

    # .ps1 必須是 UTF-8 with BOM：Windows PowerShell 5.1 沒有 BOM 就用 ANSI
    # （zh-TW 是 cp950）讀檔，裡面的中文全變亂碼。這台有沒有 pwsh 7 決定
    # .bat 走哪個直譯器，所以**跑起來會過不代表別台會過**——直接驗位元組。
    head = (root / "twcrawl-console.ps1").read_bytes()[:3]
    assert head == b"\xef\xbb\xbf", \
        f"twcrawl-console.ps1 要存成 UTF-8 with BOM，實際開頭是 {head!r}"
    # .bat 反過來：必須純 ASCII（cmd 逐位元組用 OEM codepage 解析，見下）
    bat = (root / "twcrawl-console.bat").read_bytes()
    assert max(bat) < 128, "twcrawl-console.bat 必須是純 ASCII，中文一律放 .ps1"

    if sys.platform != "win32":
        print("✓ 一鍵啟動器：非 Windows，只驗了編碼")
        return

    with TemporaryDirectory() as td:
        td = Path(td)
        for name in ("twcrawl-console.bat", "twcrawl-console.ps1"):
            shutil.copyfile(root / name, td / name)
        # 這個暫存目錄沒有 .venv，所以走的是「不在工作區」那條路
        p = subprocess.run(
            [str(td / "twcrawl-console.bat")], cwd=str(td),
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)

    out = p.stdout + p.stderr
    assert p.returncode != 0, f"找不到 venv 該回非零，卻回 {p.returncode}"
    assert "找不到" in out and "虛擬環境" in out, \
        f"該給人話提示且中文未被 cmd 的 codepage 切壞，實際輸出：{out!r}"
    print("✓ 一鍵啟動器：缺 venv 給人話並回非零（中文未被 cmd 解析壞）")


def test_jobs_runner_recovers_when_job_cannot_start():
    """工作起不來也一定要收尾，否則 runner 永久 Busy、控制台再也按不動。

    這是 `_run` 那個 finally 的理由：只要 returncode 留在 None，state 就永遠
    是 running，Runner.start 從此每次都拋 Busy，只能重啟 serve 才救得回來。
    用「cwd 不存在」來逼出啟動失敗——不必真的跑起一個行程，判定是確定的。
    """
    import time

    from twcrawl import jobs

    runner = jobs.Runner()
    job = runner.start("export", Path("D:/絕不存在的工作區/nope"))
    for _ in range(100):                      # 啟動失敗是同步的，很快
        if job.state != "running":
            break
        time.sleep(0.05)
    assert job.state == "failed", f"起不來的工作該收在 failed，卻是 {job.state}"
    assert job.returncode is not None, "returncode 留在 None 會讓 runner 卡死"

    # 真正要守的不變式：前一個工作收尾後，下一個要能開得起來
    again = runner.start("export", Path("D:/絕不存在的工作區/nope"))
    assert again.id != job.id, "前一個工作結束後應該能再開下一個"

    try:
        runner.start("不存在的工作", Path("."))
        raise AssertionError("白名單外的名稱應該被擋下")
    except KeyError:
        pass
    print("✓ jobs：工作起不來也會收尾（runner 不會永久 Busy）、白名單擋住未知名稱")


def test_operator_signal_handoff():
    """登入交接：控制台按下「我已登入」，正在等的子行程就要往下跑。

    這條鏈跨兩個行程，中間只有環境變數與 stdout 可用：
      子行程 `wait_for_operator` 印出標記 → jobs 認得標記、控制台亮出按鈕 →
      按鈕把訊號檔寫到 `job.done_file` → 子行程看到檔案、收掉、繼續。
    兩端各自手打一份字串的失敗方式是**靜默**的（按鈕從此不出現，而終端機那條
    路照樣會動），所以這裡把「同一份定義」與兩端的行為一起釘住。
    """
    import contextlib as ctxlib
    import io
    import os
    import subprocess
    import threading
    import time

    from twcrawl import jobs, operator_signal
    from twcrawl.browser import wait_for_operator

    # ① 等待的一端：印出機器可讀標記；訊號檔一出現就返回，並且**收掉**它
    with TemporaryDirectory() as td:
        flag = Path(td) / "done"
        os.environ[operator_signal.ENV_DONE_FILE] = str(flag)
        try:
            threading.Timer(0.6, lambda: operator_signal.send(flag)).start()
            buf = io.StringIO()
            with ctxlib.redirect_stdout(buf):
                wait_for_operator("（測試）")
            assert operator_signal.AWAITING_LINE in buf.getvalue(), \
                f"等待人工時要印出機器可讀標記，實際輸出：{buf.getvalue()!r}"
            assert not flag.exists(), \
                "訊號檔用完要收掉，否則下一次等待會被上一次留下的檔案立刻放行"
        finally:
            os.environ.pop(operator_signal.ENV_DONE_FILE, None)

    # ② 按按鈕的一端：認得標記才亮按鈕，按下去寫的是子行程正在等的那條路徑
    with TemporaryDirectory() as td:
        job = jobs.Job(1, "login", ["login"], "登入", Path(td) / "done")
        assert not job.snapshot()["awaiting"]
        assert job.signal_operator() is False, \
            "沒在等人工就不該放訊號檔——提早放下去，login 真的問起時會被立刻放行"
        job.add(f"{operator_signal.AWAITING_LINE} 等待訊號檔案出現：…")
        assert job.snapshot()["awaiting"] is True, "看到標記才亮得出「我已登入」"
        assert job.signal_operator() is True
        assert job.done_file.exists(), "「我已登入」要真的產生訊號檔"
        assert job.snapshot()["awaiting"] is False, "按過一次就不再等了"

    # ③ 接線：runner 交給子行程的環境變數，就是按鈕會寫的那條路徑。
    #    不真的起行程——換掉 jobs 命名空間裡的 subprocess 參照，錄下呼叫參數。
    seen: dict = {}

    class _FakeSubprocess:
        PIPE, STDOUT, DEVNULL = (subprocess.PIPE, subprocess.STDOUT,
                                 subprocess.DEVNULL)
        run = staticmethod(subprocess.run)

        @staticmethod
        def Popen(argv, **kw):
            seen["argv"] = argv
            seen.update(kw)
            raise OSError("（測試）不真的起行程")

    real_subprocess = jobs.subprocess
    jobs.subprocess = _FakeSubprocess
    try:
        runner = jobs.Runner()
        job = runner.start("update", Path.cwd())
        for _ in range(100):                  # 啟動失敗是同步的，很快
            if job.state != "running":
                break
            time.sleep(0.05)
    finally:
        jobs.subprocess = real_subprocess

    assert seen["env"][operator_signal.ENV_DONE_FILE] == str(job.done_file), \
        "子行程等的路徑必須就是按鈕會寫的那條，否則「我已登入」按了沒反應"
    assert seen["stdin"] is subprocess.DEVNULL, (
        "stdin 一定要斷開：serve 若是從終端機起的，子行程會繼承那個 tty，"
        "update 的備份密碼 getpass 就停在沒人看的視窗上等輸入，"
        "而控制台永遠停在「執行中」")
    print("✓ 人工交接：標記→按鈕→訊號檔→子行程繼續（兩端同一份定義、stdin 斷開）")


def _pid_alive(pid: int) -> bool:
    """這個 pid 還活著嗎。

    Windows 不能用 `os.kill(pid, 0)` 探測——CPython 在那裡會真的去終止行程。
    """
    import os
    import subprocess

    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_jobs_cancel_kills_process_tree():
    """中止不能只殺得到直接的子行程，否則會留下孤兒。

    長工真正吃資源的是 Playwright 起的 Chromium，它是子行程的**子行程**。
    只 terminate 父的話，頁面顯示「已中止」，工作管理員裡卻還有一票 chrome.exe
    抱著登入中的頁面——正是驗收條件說的「半死的子行程」。
    """
    import subprocess
    import time

    from twcrawl import jobs

    # 造一棵真的樹：父再生一個孫，把孫的 pid 印出來之後兩個都長睡
    child_src = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
        "print(p.pid, flush=True);"
        "time.sleep(120)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_src], stdout=subprocess.PIPE, text=True,
        **({} if sys.platform == "win32" else {"start_new_session": True}))
    try:
        grandchild = int(proc.stdout.readline().strip())
        assert _pid_alive(grandchild), "孫行程應該還活著（這是測試的前提）"

        jobs._kill_tree(proc)

        for _ in range(100):
            if proc.poll() is not None and not _pid_alive(grandchild):
                break
            time.sleep(0.05)
        assert proc.poll() is not None, "直接的子行程沒被殺掉"
        assert not _pid_alive(grandchild), (
            f"孫行程（pid {grandchild}）還活著——中止留下了孤兒，"
            "真實情境下那就是還開著的 Chromium")
    finally:
        try:
            proc.kill()
            proc.stdout.close()
        except Exception:
            pass

    # 中止比行程起來早一步按下：attach 要把這件事回報給 _run，否則沒有人去殺
    # 它，工作會一路跑完而頁面顯示已中止
    with TemporaryDirectory() as td:
        job = jobs.Job(1, "update", ["update"], "每月例行", Path(td) / "done")
        assert job.cancel() is True
        assert job.attach(object()) is True, \
            "先按中止、行程才起來的話，attach 必須回報「已經被中止了」"
        job.finish(1)
        assert job.state == "cancelled", "使用者自己按的中止不該顯示成失敗"
        assert job.cancel() is False, \
            "已結束的工作沒東西可中止（端點據此回 409，而不是假裝成功）"
    print("✓ 中止：整棵行程樹收掉（不留孤兒瀏覽器）、搶在啟動之前按也擋得住")


def test_serve_jobs_runs_export_in_subprocess():
    """控制台的工作端點：真的起子行程跑 export，輸出收得到、同時只准一個。

    刻意用真的子行程而不是 stub——「子行程跑的是最新程式碼、不會用到 serve
    進程裡的舊模組」正是選這個做法的理由（jobs.py 開頭），stub 掉就什麼都
    沒驗到。順帶把白名單與跨來源防護一起釘住：這兩個端點會改檔案、會起
    行程，不該讓別的網頁按得動。
    """
    import threading
    import time
    import urllib.error
    import urllib.request

    from twcrawl import serve as serve_mod
    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as td:
        ws = Workspace(Path(td))
        conn = db.connect(ws.db)
        try:
            db.upsert_invoices(conn, [
                an_invoice("JJ1", "2026-05-01", "工作測試店"),
            ])
        finally:
            conn.close()

        httpd = serve_mod.make_server(ws, port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            port = httpd.server_address[1]

            code, j = _post_json(port, "/api/jobs", {"cmd": "export"})
            assert code == 202 and j["ok"], f"啟動工作失敗：{code} {j}"

            # 「同時只准一個」要在工作確實還在跑的時候問才算數。先讀一次狀態
            # 把這件事變成斷言——原本是默默假設「子行程啟動夠慢」，那是競態，
            # 哪天啟動變快就會變成偶發失敗，而且訊息看不出真正原因。
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/jobs/current") as r:
                live = json.loads(r.read().decode("utf-8"))["job"]
            assert live["state"] == "running", (
                f"工作在第一次讀狀態時就已經結束（{live['state']}），"
                "下面的 409 斷言便不成立——請改用更慢的工作重測")
            code2, j2 = _post_json(port, "/api/jobs", {"cmd": "export"})
            assert code2 == 409, f"同時只該准一個工作，卻收到 {code2}：{j2}"

            deadline = time.time() + 180
            cursor, lines, state = 0, [], "running"
            while time.time() < deadline:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}"
                        f"/api/jobs/current?since={cursor}") as r:
                    snap = json.loads(r.read().decode("utf-8"))["job"]
                lines += snap["lines"]
                cursor, state = snap["next"], snap["state"]
                if state != "running":
                    break
                time.sleep(0.2)

            assert state == "done", f"工作沒有成功收尾：{state}／{lines}"
            data_js = ws.out / "data.js"
            assert data_js.exists(), "export 應該真的產出 data.js"
            assert "工作測試店" in data_js.read_text(encoding="utf-8")
            assert any("儀表板" in ln for ln in lines), \
                f"應該收得到子行程的輸出，實際只有：{lines}"

            # 端點收的是工作名稱不是 argv：白名單外的名字進不去
            code3, j3 = _post_json(port, "/api/jobs", {"cmd": "backup"})
            assert code3 == 400 and not j3["ok"], f"白名單沒擋住：{code3} {j3}"

            # 使用者開著 serve 時，別的網頁不該按得動這些端點。POST 會起
            # 行程，GET 會吐工作輸出（含使用者資料）——兩個都要擋，不是只擋
            # 會寫入的那個
            code4, _ = _post_json(port, "/api/jobs", {"cmd": "export"},
                                  origin="http://evil.example")
            assert code4 == 403, f"跨來源 POST 該被擋，卻收到 {code4}"
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/jobs/current",
                headers={"Origin": "http://evil.example"})
            try:
                urllib.request.urlopen(req)
                raise AssertionError("跨來源 GET 也該被擋")
            except urllib.error.HTTPError as e:
                assert e.code == 403, f"跨來源 GET 該回 403，卻收到 {e.code}"

            # 沒有在等人工的時候放訊號檔，會讓 login 真的問起時被立刻放行；
            # 沒有在跑的時候中止則是無事可做。兩個都要說實話，不能假裝成功
            code5, j5 = _post_json(port, "/api/jobs/signal", {})
            assert code5 == 409, f"沒在等人工時的 signal 該回 409：{code5} {j5}"
            code6, j6 = _post_json(port, "/api/jobs/cancel", {})
            assert code6 == 409, f"沒有工作在跑時的 cancel 該回 409：{code6} {j6}"

            # 參數是**驗過形狀**才進 argv 的（#21）：月份不合格回人話 400
            code7, j7 = _post_json(
                port, "/api/jobs",
                {"cmd": "fetch", "params": {"from": "2026-13", "to": "2026-01"}})
            assert code7 == 400 and "YYYY-MM" in j7["error"], \
                f"月份不合格該回人話 400，實際：{code7} {j7}"

            # 合格的參數真的接到 argv 上。這個工作區沒有登入狀態，所以子行程
            # 會以「找不到登入狀態」收在 failed——那正好證明它真的跑到了 fetch
            code8, j8 = _post_json(
                port, "/api/jobs",
                {"cmd": "fetch", "params": {"from": "2026-01", "to": "2026-02"}})
            assert code8 == 202, f"合格的 fetch 參數該被接受：{code8} {j8}"
            assert "2026-01" in j8["job"]["title"], \
                f"標題要說出抓的區間，實際：{j8['job']['title']!r}"

            deadline = time.time() + 180
            cursor, lines, state = 0, [], "running"
            while time.time() < deadline:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}"
                        f"/api/jobs/current?since={cursor}") as r:
                    snap = json.loads(r.read().decode("utf-8"))["job"]
                lines += snap["lines"]
                cursor, state = snap["next"], snap["state"]
                if state != "running":
                    break
                time.sleep(0.2)
            assert state == "failed", f"沒有登入狀態的 fetch 該失敗：{state}／{lines}"
            assert any("登入" in ln for ln in lines), \
                f"失敗原因該是缺登入狀態，實際輸出：{lines}"
        finally:
            httpd.shutdown()
            httpd.server_close()
    print("✓ serve：/api/jobs 起子行程跑 export／fetch（輸出可取、同時只准一個、"
          "白名單、參數驗形狀、跨來源與 signal／cancel 的 409）")


def test_control_page_login_handoff_and_cancel():
    """控制台的長工三件事（#21）：登入交接、中止、fetch 的區間參數。

    工作端點是 stub 的（真工作由 test_serve_jobs_runs_export_in_subprocess
    驗），這裡看的是頁面：等人工的時候有沒有把按鈕亮出來、按下去送的是哪個
    端點、中止之後狀態燈說的是「已中止」而不是「失敗」。
    """
    import threading

    from twcrawl import serve as serve_mod
    from twcrawl.workspace import Workspace

    # 頁面的所有 POST 都記下來；GET 一律回 window.__job（測試逐段換掉它）
    stub = """
      () => {
        window.__posts = [];
        window.__job = {id: 7, name: "update", title: "每月例行",
                        state: "running", returncode: null, awaiting: true,
                        lines: ["=== 1/7 login ==="], next: 1, dropped: 0};
        window.fetch = async (url, opts) => {
          if (opts && opts.method === "POST") {
            window.__posts.push({url: url, body: JSON.parse(opts.body)});
            if (url === "/api/jobs") {
              return {status: 202, json: async () => ({ok: true, job: window.__job})};
            }
            // 真的 server 在 signal 之後就不再回報 awaiting——stub 也照做，
            // 否則下一輪輪詢會把交接區再亮回來
            if (url === "/api/jobs/signal") window.__job.awaiting = false;
            return {status: 200, json: async () => ({ok: true})};
          }
          return {status: 200, json: async () => ({ok: true, job: window.__job})};
        };
      }
    """

    with TemporaryDirectory() as td:
        ws = Workspace(Path(td))
        _stage_pages(ws, a_payload())
        with browser_context(session_file=None, headed=False) as ctx:
            httpd = serve_mod.make_server(ws, port=0)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                port = httpd.server_address[1]
                page = ctx.new_page()
                errs = []
                page.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
                page.goto(f"http://127.0.0.1:{port}/control.html")
                page.evaluate(stub)
                page.click("#run-update")

                # 等人工的時候：交接區出現，狀態燈說的是「等你登入」而不是
                # 「執行中」——後者會讓人以為機器正在忙，其實它在等自己
                page.wait_for_selector("#handoff:visible", timeout=5000)
                assert "等你登入" in page.inner_text("#state"), \
                    f"卡在人工交接時狀態燈要說實話，實際：{page.inner_text('#state')!r}"

                page.click("#done-login")
                page.wait_for_selector("#handoff", state="hidden", timeout=5000)

                # 工作繼續跑（不再等人工），中止鍵在、按下去送 cancel 端點
                page.wait_for_selector("#cancel:visible", timeout=5000)
                page.click("#cancel")
                page.evaluate(
                    "() => { window.__job.state = 'cancelled';"
                    " window.__job.returncode = 1; }")
                page.wait_for_selector("#state:has-text('已中止')", timeout=5000)
                assert "失敗" not in page.inner_text("#state"), \
                    "自己按的中止不是失敗"
                assert page.eval_on_selector(
                    "#state", "e => !e.classList.contains('failed')"), \
                    "中止不該套用失敗的樣式"
                assert not page.is_disabled("#run-update"), \
                    "工作結束後按鈕要能再按"

                # fetch：頁面送的是具名參數（端點收的不是 argv），值取自輸入框
                page.fill("#from", "2026-03")
                page.fill("#to", "2026-05")
                page.click("#run-fetch")
                page.wait_for_timeout(200)
                posts = page.evaluate("() => window.__posts")
                paths = [p["url"] for p in posts]
                assert paths == ["/api/jobs", "/api/jobs/signal",
                                 "/api/jobs/cancel", "/api/jobs"], \
                    f"送出的端點順序不對：{paths}"
                assert posts[0]["body"]["cmd"] == "update"
                assert posts[-1]["body"] == {
                    "cmd": "fetch", "params": {"from": "2026-03", "to": "2026-05"}}, \
                    f"fetch 該帶區間參數，實際：{posts[-1]['body']}"
                assert not errs, f"control.html 有 JS 錯誤：{errs}"
                page.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
    print("✓ 控制台：等人工時亮出「我已登入」（送 signal）、中止不算失敗、"
          "fetch 帶具名區間參數")


def test_control_page_modes_and_output_escaping():
    """控制台頁：file:// 只給說明；serve 之下顯示工作三態；輸出不進 DOM 結構。

    工作回應是 stub 的——這裡測的是頁面怎麼呈現狀態與輸出，工作本身由
    test_serve_jobs_runs_export_in_subprocess 驗。兩者刻意分開：一個炸了
    才看得出是後端還是頁面。
    """
    import threading

    from twcrawl import serve as serve_mod
    from twcrawl.workspace import Workspace

    hostile = '<b>惡意</b><img src=x onerror="alert(1)">'
    # POST 回進行中（此刻還沒有輸出，與真實端點一致）、GET 回收尾＋整份輸出
    stub = """
      ([hostile, endState, rc, jobId]) => {
        window.fetch = async (url, opts) => {
          const post = opts && opts.method === "POST";
          const job = post
            ? {id: jobId, name: "export", state: "running", returncode: null,
               lines: [], next: 0, dropped: 0}
            : {id: jobId, name: "export", state: endState, returncode: rc,
               lines: [hostile, "收尾一行"], next: 2, dropped: 0};
          return {status: 200, json: async () => ({ok: true, job})};
        };
      }
    """
    # 409＝別的分頁已經在跑。這不是「工作失敗」，輸出也不該被清掉
    busy_stub = """
      () => {
        window.fetch = async (url, opts) => {
          if (opts && opts.method === "POST") {
            return {status: 409, json: async () =>
              ({ok: false, error: "「export」還在跑"})};
          }
          return {status: 200, json: async () => ({ok: true, job:
            {id: 9, name: "export", state: "running", returncode: null,
             lines: ["別人的工作"], next: 1, dropped: 0}})};
        };
      }
    """

    with TemporaryDirectory() as td:
        ws = Workspace(Path(td))
        out = _stage_pages(ws, a_payload())
        with browser_context(session_file=None, headed=False) as ctx:
            # file://：給說明，不給按了沒反應的按鈕
            page, errs = _open(ctx, out, "control.html")
            assert not errs, f"control.html（file://）有 JS 錯誤：{errs}"
            assert page.is_visible("#need-serve"), \
                "file:// 之下應顯示「需要 serve 模式」"
            assert not page.is_visible("#panel"), \
                "file:// 之下不該給按了沒反應的按鈕"
            page.close()

            httpd = serve_mod.make_server(ws, port=0)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                port = httpd.server_address[1]
                page = ctx.new_page()
                errs = []
                page.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
                page.goto(f"http://127.0.0.1:{port}/control.html")
                page.wait_for_timeout(300)
                assert page.is_visible("#panel"), "serve 之下應該有控制面板"
                assert not page.is_visible("#need-serve")

                # 成功：running（輸出夾帶惡意字串）→ done
                page.evaluate(stub, [hostile, "done", 0, 1])
                page.click("#run-export")
                page.wait_for_timeout(120)
                assert "執行中" in page.inner_text("#state"), \
                    f"工作進行中該顯示執行中，實際是 {page.inner_text('#state')!r}"
                assert page.is_disabled("#run-export"), "進行中不該還能再按"
                page.wait_for_selector("#state:has-text('完成')", timeout=5000)
                assert page.eval_on_selector(
                    "#out", "e => e.querySelector('b, img') === null"), \
                    "工作輸出不得被當成 HTML 解讀"
                assert hostile in page.inner_text("#out"), \
                    "惡意字串應原樣顯示為文字"

                # 失敗：結束碼要露出來，按鈕要能再按
                page.evaluate(stub, [hostile, "failed", 3, 2])
                page.click("#run-export")
                page.wait_for_selector("#state:has-text('失敗')", timeout=5000)
                assert "3" in page.inner_text("#state"), "失敗要顯示結束碼"
                assert not page.is_disabled("#run-export"), \
                    "工作結束後按鈕要能再按"

                # 409：別的分頁在跑。不得顯示成「失敗」，也不得清掉輸出——
                # 那會讓使用者以為自己的工作掛了，而真正在跑的那個從此看不到
                page.evaluate(busy_stub)
                page.click("#run-export")
                page.wait_for_selector("#state:has-text('已有工作在跑')",
                                       timeout=5000)
                assert "失敗" not in page.inner_text("#state"), \
                    "忙碌不是失敗，狀態燈不該講成失敗"
                assert page.eval_on_selector(
                    "#state", "e => !e.classList.contains('failed')"), \
                    "忙碌不該套用失敗的樣式"
                page.wait_for_selector("#out:has-text('別人的工作')",
                                       timeout=5000)
                assert not errs, f"control.html 有 JS 錯誤：{errs}"
                page.close()

                # 五頁 → 控制台的入口（ui.js 注入，只在 serve 模式出現，
                # 所以走 file:// 的 golden 快照天生看不到它）
                for name in ["dashboard.html", "query.html", "fda.html",
                             "year.html", "map.html"]:
                    p = ctx.new_page()
                    p.goto(f"http://127.0.0.1:{port}/{name}")
                    p.wait_for_timeout(600)
                    assert p.eval_on_selector_all("a.ctl", "es => es.length") == 1, \
                        f"{name} 在 serve 模式下應該有一個控制台連結"
                    p.close()

                # payload 壞掉時**更**需要進得去控制台（要按重生報表）。四頁的
                # .sub 是 render 才建的，TW.page 在這種情形直接 return，所以
                # 注入的掛點後備鏈在這裡才真的被用到
                (out / "data.js").write_text("window.TWCRAWL_DATA = {};\n",
                                             encoding="utf-8")
                p = ctx.new_page()
                p.goto(f"http://127.0.0.1:{port}/dashboard.html")
                p.wait_for_timeout(600)
                assert p.eval_on_selector_all("a.ctl", "es => es.length") == 1, \
                    "data.js 殘缺時仍要進得去控制台——那正是最需要重生的時候"
                p.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
    print("✓ 控制台頁：file:// 給說明、三態＋忙碌不誤示為失敗、"
          "輸出不進 DOM 結構、五頁入口（含 payload 殘缺）")


def test_local_rules_validation():
    from twcrawl.categories import Classifier, load_local_config

    with TemporaryDirectory() as td:
        td = Path(td)
        bad = td / "categories.local.json"
        bad.write_text('{ "rules": {"小巷": "餐飲",} }', encoding="utf-8")
        try:
            Classifier(local_path=bad)
            raise AssertionError("語法錯應該給人話錯誤")
        except SystemExit as e:
            assert "語法錯誤" in str(e) and "行" in str(e), str(e)

        dup = td / "dup.json"
        dup.write_text('{ "rules": {"小巷": "餐飲", "小巷": "咖啡"} }',
                       encoding="utf-8")
        try:
            load_local_config(dup)
            raise AssertionError("重複鍵應該報錯")
        except SystemExit as e:
            assert "小巷" in str(e) and "重複" in str(e), str(e)

        typ = td / "typ.json"
        typ.write_text('{ "unnecessary": "手搖飲" }', encoding="utf-8")
        try:
            load_local_config(typ)
            raise AssertionError("型別錯應該報錯")
        except SystemExit as e:
            assert "unnecessary" in str(e), str(e)

        itemtyp = td / "itemtyp.json"
        itemtyp.write_text('{ "item_rules": ["無鉛汽油"] }', encoding="utf-8")
        try:
            load_local_config(itemtyp)
            raise AssertionError("item_rules 型別錯應該報錯")
        except SystemExit as e:
            assert "item_rules" in str(e), str(e)

        eat = td / "eat.json"
        eat.write_text('{ "eatery": "麵食" }', encoding="utf-8")
        try:
            load_local_config(eat)
            raise AssertionError("eatery 型別錯應該報錯")
        except SystemExit as e:
            assert "eatery" in str(e), str(e)
    print("✓ 規則檔防呆：語法錯（含行號）、重複鍵、型別錯都給人話")


def test_budget_loader_guards():
    """budget.local.json 防呆（issue #12）：壞檔給人話、訊息不印金額值。"""
    from twcrawl.export import load_budget

    with TemporaryDirectory() as td:
        p = Path(td) / "budget.local.json"
        assert load_budget(p) is None, "沒有檔案就是沒有預算，不是錯誤"
        for text in ("{}", '{"monthly": null}',
                     '{"monthly": null, "unnecessary": null}'):
            p.write_text(text, encoding="utf-8")
            assert load_budget(p) is None, f"空設定 {text} 該回 None（無磚）"

        p.write_text('{"monthly": 25000}', encoding="utf-8")
        assert load_budget(p) == {"monthly": 25000.0, "unnecessary": None}
        p.write_text('{"unnecessary": 3000.5}', encoding="utf-8")
        assert load_budget(p) == {"monthly": None, "unnecessary": 3000.5}, \
            "兩者可只設其一"

        bad = [
            ("[]", "最外層"),
            ('{"montly": 1}', "不認得的鍵"),      # 打錯字不能靜默變成沒設定
            ('{"monthly": 800, "monthly": 900}', "重複"),
            ('{"monthly": -1}', "正數"),
            ('{"monthly": 0}', "正數"),
            ('{"monthly": true}', "正數"),        # bool 是 int 的子類，要擋
            ('{"monthly": "25000"}', "正數"),
        ]
        for text, want in bad:
            p.write_text(text, encoding="utf-8")
            try:
                load_budget(p)
                raise AssertionError(f"{text} 該報錯卻通過")
            except SystemExit as e:
                assert want in str(e), (text, str(e))

        # 行號斷言只驗「有行號」——尾逗號的訊息與行號在 Py3.10~3.13 不同
        p.write_text('{\n "monthly": 800,\n}', encoding="utf-8")
        try:
            load_budget(p)
            raise AssertionError("語法錯該報錯卻通過")
        except SystemExit as e:
            assert "語法錯誤" in str(e) and " 行" in str(e), str(e)
    print("✓ 預算檔防呆：語法錯（含行號）、未知鍵、型別錯、非正數都給人話")


def test_export_match_details():
    from twcrawl import export
    from twcrawl.categories import Classifier
    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as td:
        td = Path(td)
        ws = Workspace(td)
        ws.ensure_out()
        ws.match_report.write_text(
            "﻿level,inv_num,inv_date,invoice_side,fda_side,source\n"
            "店家,AA1,2026-05-01,富利餐飲,富利餐飲股份有限公司,edible_oil\n",
            encoding="utf-8")
        conn = db.connect(ws.db)
        try:
            db.upsert_invoices(conn, [
                an_invoice("AA1", "2026-05-01", "富利餐飲")])
            cl = Classifier(ws.rules)
            payload = export.build_payload(conn, ws, cl)
            # 層級直方圖不進 payload——食安頁從 matches 導出（唯一編碼）
            assert "match" not in payload["fda"], payload["fda"].keys()
            assert len(payload["fda"]["matches"]) == 1
            m = payload["fda"]["matches"][0]
            assert m["level"] == "店家" and m["fda"].startswith("富利"), m
            assert m["date"] == "2026-05-01" and m["source"] == "edible_oil"
            assert m["num"] == "AA1", "命中要帶發票號碼（食安頁回鏈查詢頁）"
            src = next(s for s in payload["fda"]["sources"]
                       if s["key"] == "edible_oil")
            assert src["label"] == "中聯油脂案" and src["kind"] == "事件"
            assert src["hits"] == 1, "來源要帶命中數（食安頁總覽用）"

            # 沒有報告的工作區：不該撿到別處的檔案
            empty = Workspace(td / "另一個工作區")
            empty.ensure_out()
            none = export.build_payload(conn, empty, cl)
            assert none["fda"]["matches"] is None, "沒報告時不該撿到別處的檔案"
        finally:
            conn.close()
    print("✓ export：match 命中明細進 payload（月報 FDA 卡）、報告路徑由工作區決定")


def test_fixed_spend_detection():
    from twcrawl.export import _detect_fixed

    rows = (
        [{"date": f"2026-{m:02d}-07", "seller": "GOOGLE", "amount": 479.0}
         for m in range(1, 8)]                                  # 每月 7 日
        + [{"date": "2026-01-28", "seller": "PLAY", "amount": 75.0},
           {"date": "2026-02-27", "seller": "PLAY", "amount": 75.0},
           {"date": "2026-03-27", "seller": "PLAY", "amount": 75.0}]  # 4 月起停
        + [{"date": f"2026-07-{d:02d}", "seller": "手搖", "amount": 60.0}
           for d in (1, 3, 5, 7, 9, 11)]                        # 高頻日常
        + [{"date": "2026-07-26", "seller": "全聯", "amount": 999.0}]
    )
    found = _detect_fixed(rows)
    g = next(f for f in found if f["seller"] == "GOOGLE")
    assert g["active"] and 25 <= g["periodDays"] <= 35, g
    # 週期標籤在這裡算完：門檻是 _detect_fixed 的實作細節，讓查詢頁重述
    # 一次就會漂（原本 JS 那份的最後一支「約 N 天」永遠不可達）
    assert g["periodLabel"] == "每月", g
    assert g["next"] and g["next"].startswith("2026-08"), "進行中要推下次預估"
    p = next(f for f in found if f["seller"] == "PLAY")
    assert not p["active"] and p["next"] is None, "超過 1.6 週期未出現＝疑似已停"
    assert not any(f["seller"] == "手搖" for f in found), "高頻日常不屬固定支出"
    assert found[0]["seller"] == "GOOGLE", "進行中應排在已停之前"
    print("✓ 固定支出偵測：月訂閱、已停判定、高頻排除（export 共用、月報磚同源）")


# ------------------------------------------------------------- 對獎 --

# 擬真 invoice.etax.nat.gov.tw 結構：導覽選單帶別期字樣（不可信）、
# 頭獎號碼拆成前 5＋後 3 兩個相鄰 span、desktop/mobile 兩份重複、
# 期別只能由 tfoot 領獎期間反推。號碼為虛構。
_LOTTERY_HTML = """
<html><body>
<ul><li><a href="lastNumber.html" title="115年03-04月中獎號碼單">115年03-04月中獎號碼單</a></li></ul>
<table><tbody>
<tr> <td headers="th01" class="text-center">特別獎</td> <td headers="th02">
<p class="etw-tbiggest"><span class="fw-bold etw-color-red">11223344</span></p>
<p class="mb-0">同期統一發票收執聯8位數號碼與特別獎號碼相同者獎金1,000萬元</p> </td> </tr>
<tr> <td headers="th01" class="text-center">特獎</td> <td headers="th02">
<p class="etw-tbiggest"><span class="fw-bold etw-color-red">55667788</span></p>
<p class="mb-0">同期統一發票收執聯8位數號碼與特獎號碼相同者獎金200萬元</p> </td> </tr>
<tr> <td headers="th01" class="text-center">頭獎</td> <td headers="th02">
<p class="etw-tbiggest mb-md-4"> <span class="fw-bold">12345</span><span class="fw-bold etw-color-red">678</span></p>
<p class="etw-tbiggest mb-md-4"> <span class="fw-bold">23456</span><span class="fw-bold etw-color-red">789</span></p>
<p class="etw-tbiggest mb-md-4"> <span class="fw-bold">34567</span><span class="fw-bold etw-color-red">890</span></p>
<p class="mb-0">同期統一發票收執聯8位數號碼與頭獎號碼相同者獎金20萬元</p> </td> </tr>
<tr> <td headers="th01" class="text-center">增開六獎</td> <td headers="th02">
<p class="etw-tbiggest"><span class="fw-bold etw-color-red">217</span></p>
<p class="mb-0">同期統一發票收執聯末3位數號碼與增開六獎號碼相同者各得獎金2百元</p> </td> </tr>
</tbody>
<tfoot><tr><td colspan="2">領獎期間自115年08月06日起至115年11月05日止</td></tr></tfoot>
</table>
<div class="etw-mobile"><table><tbody>
<tr> <td class="text-center">特別獎</td> <td><p class="etw-tbiggest"><span>11223344</span></p>
<p>同期統一發票收執聯8位數號碼與特別獎號碼相同者獎金1,000萬元</p></td> </tr>
</tbody></table></div>
</body></html>
"""


def test_lottery_parse_draw():
    from twcrawl import lottery

    d = lottery.parse_draw(_LOTTERY_HTML)
    assert d["period"] == "11505", d  # 領獎 08 月起 → 05-06 月期，不受選單字樣干擾
    assert d["special"] == "11223344" and d["grand"] == "55667788", d
    assert d["first"] == ["12345678", "23456789", "34567890"], "拆 span 的號碼要黏回"
    assert d["extra"] == ["217"], "增開六獎不可誤抓規則文字裡的數字"
    assert d["claim_start"] == "2026-08-06" and d["claim_end"] == "2026-11-05", d
    assert lottery.period_months("11505") == ["2026-05", "2026-06"]
    # 1-2 月期領獎起於次年 2 月 → 期別要跨年回推
    cross = _LOTTERY_HTML.replace("115年08月06日起至115年11月05日",
                                  "116年02月06日起至116年05月05日")
    assert lottery.parse_draw(cross)["period"] == "11511"
    assert lottery.next_draw("11505") == ("11507", "2026-09-25")
    assert lottery.next_draw("11511") == ("11601", "2027-03-25"), "下期開獎跨年"
    try:
        lottery.parse_draw("<html><body>改版後的空頁</body></html>")
        raise AssertionError("結構不符要 raise，不能默默錯對")
    except ValueError:
        pass
    print("✓ 對獎：號碼頁解析（期別反推、span 黏回、跨年期別、改版防呆）")


def test_lottery_match_number():
    from twcrawl import lottery

    d = {"special": "11223344", "grand": "55667788",
         "first": ["12345678", "23456789", "34567890"], "extra": ["217"]}
    assert lottery.match_number("11223344", d) == ("特別獎", 10_000_000)
    assert lottery.match_number("55667788", d) == ("特獎", 2_000_000)
    assert lottery.match_number("12345678", d) == ("頭獎", 200_000)
    assert lottery.match_number("99945678", d) == ("四獎", 4_000), "末 5 碼同頭獎"
    assert lottery.match_number("99996789", d) == ("五獎", 1_000), "末 4 碼同第二組"
    assert lottery.match_number("00000678", d) == ("六獎", 200)
    assert lottery.match_number("00000217", d) == ("增開六獎", 200)
    assert lottery.match_number("00000000", d) is None
    print("✓ 對獎：獎級判定（特別獎～六獎、多組頭獎、增開六獎、未中）")


def test_lottery_check_invoices():
    from twcrawl import lottery

    with TemporaryDirectory() as td:
        conn = db.connect(Path(td) / "t.sqlite")
        try:
            draw = {"period": "11505", "special": "11223344",
                    "grand": "55667788",
                    "first": ["12345678", "23456789", "34567890"],
                    "extra": ["217"],
                    "claim_start": "2026-08-06", "claim_end": "2026-11-05"}
            db.upsert_lottery_draws(conn, [draw])
            db.upsert_lottery_draws(conn, [draw])  # 冪等
            assert conn.execute(
                "select count(*) from lottery_draws").fetchone()[0] == 1
            db.upsert_invoices(conn, [
                an_invoice("AB12345678", "2026-05-10", "頭獎店", 100.0),
                an_invoice("CD00000217", "2026-06-01", "小七", 55.0),
                an_invoice("EF99999999", "2026-05-20", "沒中", 80.0),
                an_invoice("GH11223344", "2026-07-01", "期別外", 999.0),
            ])
            r = lottery.check_invoices(conn, Path(td) / "cache")
            p = r["periods"][0]
            assert p["n_invoices"] == 3, "7 月發票不屬 05-06 月期"
            prizes = {w["inv_num"]: w["prize"] for w in p["wins"]}
            assert prizes == {"AB12345678": "頭獎", "CD00000217": "增開六獎"}, prizes
            assert r["uncovered"] == 1
        finally:
            conn.close()  # Windows：先關連線才能清 TemporaryDirectory
    print("✓ 對獎：資料庫整合（期別歸屬、中獎清單、未涵蓋張數、upsert 冪等）")


def test_lottery_cloud_check():
    import gzip

    from twcrawl import lottery

    with TemporaryDirectory() as td:
        conn = db.connect(Path(td) / "t.sqlite")
        try:
            cache = Path(td) / "cache"
            cache.mkdir()
            # 雲端清冊快取（比完整字軌）：500 元獎含一張同時中傳統頭獎的票
            with gzip.open(cache / "cloud_11505_AI_D.txt.gz", "wt",
                           encoding="ascii") as f:
                f.write("AB12345678\nZZ00000001\n")
            with gzip.open(cache / "cloud_11505_AI_E.txt.gz", "wt",
                           encoding="ascii") as f:
                f.write("CD11112222\n")
            db.upsert_lottery_draws(conn, [{
                "period": "11505", "special": "99999998", "grand": "99999997",
                "first": ["12345678", "87654321", "11223344"], "extra": [],
                "claim_start": "2026-08-06", "claim_end": "2026-11-05"}])
            db.upsert_invoices(conn, [
                an_invoice("AB12345678", "2026-05-10", "兩者皆中", 100.0),
                an_invoice("CD11112222", "2026-06-02", "雲端八百", 60.0),
                an_invoice("EF33334444", "2026-05-20", "沒中", 80.0),
            ])
            r = lottery.check_invoices(conn, cache_dir=cache)
            p = r["periods"][0]
            assert p["cloud_checked"] == ["AI_D", "AI_E"], p["cloud_checked"]
            w = {x["inv_num"]: x for x in p["wins"]}
            assert w["AB12345678"]["prize"] == "頭獎" and \
                w["AB12345678"]["also"] == "雲端五百元獎", "兩類皆中要擇高"
            assert w["CD11112222"]["prize"] == "雲端八百元獎" and \
                w["CD11112222"]["prize_amount"] == 800
            assert "EF33334444" not in w
        finally:
            conn.close()  # Windows：先關連線才能清 TemporaryDirectory
    print("✓ 對獎：雲端專屬獎（gz 快取、完整字軌比對、傳統/雲端擇高）")


def test_workspace_layout():
    """工作區：路徑全部相對 root、擷取目錄依 mtime 選最新、缺資料庫講人話。"""
    import os
    import time as _time

    from twcrawl.workspace import Workspace

    with TemporaryDirectory() as td:
        td = Path(td)
        ws = Workspace(td)
        for p in (ws.db, ws.out, ws.captures, ws.state, ws.cache, ws.rules,
                  ws.backup, ws.match_report, ws.bizreg_cache, ws.probe_out,
                  ws.state_path("einvoice"), ws.handoff_path("x")):
            assert td in p.parents or p == td, f"{p} 應在工作區內"

        # 缺資料庫：讀取型指令的前置條件要講人話
        missing = ""
        try:
            ws.require_db()
        except SystemExit as e:
            missing = str(e)
        assert "不是 twcrawl 工作區" in missing, missing

        # 沒有擷取結果也要講人話
        none = ""
        try:
            ws.latest_capture()
        except SystemExit as e:
            none = str(e)
        assert "找不到任何擷取結果" in none, none

        # new_capture：目錄形狀只有一份定義
        a = ws.new_capture("einvoice")
        assert (a / "responses").is_dir() and (a / "downloads").is_dir()

        # latest_capture 依 mtime——字典序會讓 einvoice-f… 恆勝 einvoice-2…
        _time.sleep(0.01)
        b = ws.new_capture("einvoice-fetch")
        os.utime(a, (_time.time() + 10, _time.time() + 10))  # a 明確較新
        assert ws.latest_capture() == a, \
            f"應依 mtime 選最新，實得 {ws.latest_capture().name}（另有 {b.name}）"
    print("✓ workspace：路徑全在 root 內、擷取目錄依 mtime、缺資料庫有人話")


def test_cli_wires_paths_through_workspace():
    """CLI 端到端：產物全落在工作區，跨指令的路徑彼此對得上。

    cli.py 以前覆蓋率 ~4%，而路徑接線最集中的就是它。這裡走 main([...])；
    cwd 就是工作區，那是 CLI 唯一的路徑輸入。
    """
    import os

    from twcrawl import cli
    from twcrawl.workspace import Workspace

    cwd = os.getcwd()
    with TemporaryDirectory() as d:
        ws = Workspace(Path(d))
        os.chdir(d)
        try:
            # 空目錄跑讀取型指令：要擋下並講人話，而不是默默生一個空資料庫
            refused = ""
            try:
                cli.main(["export", "--no-open"])
            except SystemExit as e:
                refused = str(e)
            assert "不是 twcrawl 工作區" in refused, refused
            assert not ws.out.exists(), "被擋下時不該留下任何目錄"

            conn = db.connect(ws.db)
            try:
                db.upsert_invoices(conn, [
                    an_invoice("ZZ11111111", "2026-05-01", "測試商行一店"),
                ])
                db.upsert_items(conn, [
                    an_item("ZZ11111111", 1, "特級沙拉油"),
                ])
                db.upsert_fda_rows(conn, [
                    {"row_hash": "z1", "source_url": "u1", "table_key": "t",
                     "page_no": 1,
                     "data": json.dumps({"業者": "測試商行",
                                         "產品/品項": "特級沙拉油18L"},
                                        ensure_ascii=False)},
                ])
            finally:
                conn.close()

            assert cli.main(["match"]) == 0
            assert ws.match_report.exists(), "match 的報告應落在工作區的 out/"

            assert cli.main(["export", "--no-open"]) == 0
            for name in ("data.js", "dashboard.html", "query.html",
                         "fda.html", "map.html"):
                assert (ws.out / name).exists(), f"{name} 應就位"
            data_js = (ws.out / "data.js").read_text(encoding="utf-8")
            assert '"matches"' in data_js and "特級沙拉油" in data_js, \
                "export 要讀得到 match 的報告——兩者的路徑必須同源推導"

            # --db 已移除：路徑一律由工作區決定
            gone = False
            try:
                cli.main(["--db", "x.sqlite", "export", "--no-open"])
            except SystemExit:
                gone = True
            assert gone, "--db 應已移除"
        finally:
            os.chdir(cwd)   # 必須先離開暫存目錄，Windows 才能清理
    print("✓ CLI：產物全落在工作區、match→export 路徑同源、--db 已移除")


# ------------------------------------------------------ 五頁煙霧測試 -----
#
# 這些測試喂頁面「合成 payload」，不指向 out/。原因不是圖方便：out/ 是真實
# 消費紀錄，斷言一旦寫上真實金額與店家名就進不了 repo（見 CLAUDE.md 的個資
# 界線）。專案史上為了 UI 打磨寫過十幾個指向 out/ 的煙霧腳本，全部只能留在
# 暫存目錄然後丟掉——這裡是它們的常駐版本。
#
# 頁面的 interface 是 window.TWCRAWL_DATA → DOM，資料庫不在其中，所以 payload
# 由 a_payload() 直接造；它與 export.build_payload 的形狀由 test_payload_contract
# 釘住，手打的 fixture 才漂不掉。

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
UPDATE_GOLDEN = "--update-golden" in sys.argv

# 頁面的可用性判準各不相同，快照的根容器也不同：地圖的 #map 由 Leaflet 管理
# （圖磚 div 隨視窗大小變動），不進快照
PAGE_ROOTS = {
    "dashboard.html": ["#app"],
    "query.html": ["#app"],
    "fda.html": ["#app"],
    "year.html": ["#app"],
    "map.html": ["#chips", "#legend", "#stat", "#nogeo", "#sellergeo"],
}


def a_payload(**overrides) -> dict:
    """合成的 data.js payload——五頁煙霧測試的輸入。

    四個月、九張發票，數字刻意小而可心算，好讓 golden 快照的 diff 讀得懂：
    電信 599×3（構成固定支出）、超市 250/200/300、手搖飲 60×2（非必要）、
    一家未分類店家（給儀表板的歸類卡）。overrides 取代整個頂層鍵。
    """
    inv = [
        ("AA1", "2026-03-05", "測試電信", "電信", 599.0, [("月租費", 599.0)]),
        ("AA2", "2026-03-12", "測試超市", "超市", 250.0, [("雞蛋", 250.0)]),
        ("AA3", "2026-04-05", "測試電信", "電信", 599.0, [("月租費", 599.0)]),
        ("AA4", "2026-04-18", "珍奶測試店", "手搖飲", 60.0, [("珍珠鮮奶茶", 60.0)]),
        ("AA5", "2026-05-05", "測試電信", "電信", 599.0, [("月租費", 599.0)]),
        ("AA6", "2026-05-10", "測試超市", "超市", 200.0, [("牛奶", 200.0)]),
        ("AA7", "2026-05-12", "神秘測試舖", "未分類", 100.0, []),
        ("AA8", "2026-06-01", "測試超市", "超市", 300.0, [("米", 300.0)]),
        ("AA9", "2026-06-05", "珍奶測試店", "手搖飲", 60.0, [("珍珠鮮奶茶", 60.0)]),
    ]
    # 狀態（issue #14）：多數「開立」；AA2 給 None（CSV 舊來源沒有狀態）、
    # AA7 給未收錄碼——原始碼要照樣顯示，不吞資訊。statusFlagged 由匯出端
    # 決定（常態與否是 Python 的事實，頁面只讀旗標）
    status = {"AA2": None, "AA7": "INVOICE0099X"}
    invoices = [
        {"num": n, "date": d, "seller": s, "category": c, "amount": a,
         "status": status.get(n, "開立"), "statusFlagged": n == "AA7",
         "items": [{"desc": desc, "qty": 1, "price": amt, "amount": amt}
                   for desc, amt in items]}
        for n, d, s, c, a, items in inv
    ]
    months = [
        {"month": "2026-03", "total": 849.0, "count": 2,
         "byCategory": {"電信": 599.0, "超市": 250.0}},
        {"month": "2026-04", "total": 659.0, "count": 2,
         "byCategory": {"電信": 599.0, "手搖飲": 60.0}},
        {"month": "2026-05", "total": 899.0, "count": 3,
         "byCategory": {"電信": 599.0, "超市": 200.0, "未分類": 100.0}},
        {"month": "2026-06", "total": 360.0, "count": 2,
         "byCategory": {"超市": 300.0, "手搖飲": 60.0}},
    ]
    unclassified = {
        "name": "神秘測試舖", "category": "未分類", "total": 100.0, "count": 1,
        "legal": None, "industry": "其他綜合零售", "address": None,
        "lat": None, "lon": None, "topItems": [],
    }
    payload = {
        "generatedAt": "2026-07-31 09:00",
        "invoiceCount": len(invoices),
        "invoices": invoices,
        "fixed": [
            {"seller": "測試電信", "amount": 599.0, "periodDays": 31,
             "periodLabel": "每月", "n": 3,
             "first": "2026-03-05", "last": "2026-05-05", "next": "2026-06-05",
             "active": True, "monthly": 588.18},
        ],
        "fixedRule": {"minCount": 3, "tolAbs": 15, "tolPct": 5,
                      "minDays": 25, "maxDays": 400, "staleFactor": 1.6},
        "months": months,
        "categories": [
            {"name": "電信", "total": 1797.0, "count": 3,
             "unnecessary": False, "slot": 1},
            {"name": "超市", "total": 750.0, "count": 3,
             "unnecessary": False, "slot": 2},
            {"name": "手搖飲", "total": 120.0, "count": 2,
             "unnecessary": True, "slot": 3},
            {"name": "未分類", "total": 100.0, "count": 1,
             "unnecessary": False, "slot": None},
        ],
        "sellers": [
            {"name": "測試電信", "category": "電信", "total": 1797.0, "count": 3,
             "legal": "測試電信股份有限公司", "industry": "電信服務",
             "address": None, "lat": None, "lon": None, "topItems": ["月租費"]},
            {"name": "測試超市", "category": "超市", "total": 750.0, "count": 3,
             "legal": "測試超市股份有限公司", "industry": "超級市場",
             "address": "臺北市測試區測試路 1 號", "lat": 25.03, "lon": 121.56,
             "topItems": ["雞蛋", "米", "牛奶"]},
            # 有座標：地圖搜尋測試要兩個圓點才驗得出「過濾剩一點」
            {"name": "珍奶測試店", "category": "手搖飲", "total": 120.0, "count": 2,
             "legal": None, "industry": None, "address": None,
             "lat": 25.04, "lon": 121.55, "topItems": ["珍珠鮮奶茶"]},
            unclassified,
        ],
        "uncategorized": [unclassified],
        "fda": {
            "rows": 12,
            "matches": [
                {"level": "店家", "date": "2026-05-10", "num": "AA6",
                 "invoice": "測試超市", "fda": "測試超市股份有限公司",
                 "source": "edible_oil"},
                {"level": "品項", "date": "2026-03-12", "num": "AA2",
                 "invoice": "雞蛋", "fda": "測試品名雞蛋", "source": "csm_news"},
            ],
            "sources": [
                {"key": "edible_oil", "label": "中聯油脂案", "kind": "事件",
                 "rows": 8, "lastSeen": "2026-07-20", "hits": 1},
                {"key": "csm_news", "label": "國內回收公告", "kind": "監測",
                 "rows": 4, "lastSeen": "2026-07-25", "hits": 1},
            ],
        },
        "lottery": {
            "periods": [
                {"period": "11503", "label": "115年03-04月",
                 "claimEnd": "2026-09-05", "nInvoices": 4,
                 "wins": [
                     {"num": "AA3", "date": "2026-04-05", "seller": "測試電信",
                      "prize": "六獎", "prizeAmount": 200, "also": None,
                      "claimEnd": "2026-09-05", "periodLabel": "115年03-04月"},
                 ]},
            ],
            "next": {"label": "115年05-06月", "drawDate": "2026-09-25",
                     "pending": 5},
        },
        # 預算磚可心算：6月 360/800=45%（剩 440）、3月 849 與 5月 899 超總額；
        # 非必要 4月/6月各 60 都破上限 50（本月超 10）
        "budget": {"monthly": 800.0, "unnecessary": 50.0},
        # 年度回顧：總額 2767、單筆最大與最貴的一天同為 AA1（599 三張同額
        # 取最早）、對獎 1 筆 200、參加 4 張（11503 期）
        "year": {
            "year": "2026", "total": 2767.0, "count": 9, "monthCount": 4,
            "monthlyAvg": 691.75,
            "byCategory": [
                {"name": "電信", "total": 1797.0},
                {"name": "超市", "total": 750.0},
                {"name": "手搖飲", "total": 120.0},
                {"name": "未分類", "total": 100.0},
            ],
            "sellers": [
                {"name": "測試電信", "total": 1797.0, "count": 3},
                {"name": "測試超市", "total": 750.0, "count": 3},
                {"name": "珍奶測試店", "total": 120.0, "count": 2},
                {"name": "神秘測試舖", "total": 100.0, "count": 1},
            ],
            "unnecessary": {"total": 120.0, "count": 2},
            "lottery": {"wins": 1, "amount": 200, "invoices": 4},
            "maxInvoice": {"num": "AA1", "date": "2026-03-05",
                           "seller": "測試電信", "category": "電信",
                           "amount": 599.0},
            "maxDay": {"date": "2026-03-05", "total": 599.0, "count": 1},
        },
    }
    payload.update(overrides)
    return payload


def _replace_strings(obj, mapping: dict[str, str]):
    """遞迴字串取代（含 dict 的鍵——byCategory 的分類名是鍵）。"""
    if isinstance(obj, str):
        for old, new in mapping.items():
            obj = obj.replace(old, new)
        return obj
    if isinstance(obj, list):
        return [_replace_strings(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {_replace_strings(k, mapping): _replace_strings(v, mapping)
                for k, v in obj.items()}
    return obj


def _stage_pages(ws, payload: dict) -> Path:
    """把 payload 與五頁模板就位到工作區的 out/，回傳該目錄。

    刻意不走 export.write_export：這裡測的是「頁面吃 payload」，
    不該把 export 的資料庫存取也拖進來。
    """
    import shutil

    from twcrawl.export import WEB_DIR

    out = ws.ensure_out()
    (out / "data.js").write_text(
        "window.TWCRAWL_DATA = "
        + json.dumps(payload, ensure_ascii=False, indent=1) + ";\n",
        encoding="utf-8")
    for src in sorted(WEB_DIR.rglob("*")):
        if src.is_dir():
            continue
        dest = out / src.relative_to(WEB_DIR)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    return out


# 1×1 透明 PNG：地圖的 OSM 圖磚在測試裡就地滿足，不對外連線、不產生 console 錯誤
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _stub_tiles(ctx) -> None:
    ctx.route("**://*.openstreetmap.org/**", lambda r: r.fulfill(
        status=200, content_type="image/png", body=_PNG_1X1))


def _open(ctx, out: Path, page_name: str, query: str = ""):
    """開一頁，回傳 (page, errs)。errs 收 pageerror 與 console.error。"""
    page = ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
    page.on("console",
            lambda m: errs.append(f"console.error: {m.text}")
            if m.type == "error" else None)
    page.goto((out / page_name).as_uri() + query)
    page.wait_for_timeout(500)
    return page, errs


_DIGEST_JS = r"""
(sels) => {
  const SVG = "http://www.w3.org/2000/svg";
  const lines = [];
  const ownText = e => {
    let t = "";
    for (const n of e.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    return t.replace(/\s+/g, " ").trim();
  };
  const label = e => {
    const cls = (e.getAttribute("class") || "").trim();
    return e.tagName.toLowerCase() +
      (cls ? "." + cls.split(/\s+/).filter(Boolean).join(".") : "");
  };
  const walk = (e, depth) => {
    const pad = "  ".repeat(depth);
    if (e.namespaceURI === SVG) {      // SVG 內部收斂成一行統計，快照才讀得懂
      const kinds = {};
      for (const d of e.querySelectorAll("*"))
        kinds[d.tagName] = (kinds[d.tagName] || 0) + 1;
      lines.push(pad + "svg [" + Object.keys(kinds).sort()
        .map(k => k + "*" + kinds[k]).join(" ") + "]");
      return;
    }
    const t = ownText(e);
    lines.push(pad + label(e) + (t ? "  " + JSON.stringify(t) : ""));
    for (const c of e.children) walk(c, depth + 1);
  };
  for (const sel of sels) {
    const root = document.querySelector(sel);
    if (!root) { lines.push(sel + "  (缺少)"); continue; }
    // 根自己的文字也要收：地圖的 #stat 是用 textContent 填的，沒有子元素
    const rt = ownText(root);
    lines.push(sel + (rt ? "  " + JSON.stringify(rt) : ""));
    for (const c of root.children) walk(c, 1);
  }
  return lines.join("\n");
}
"""


# 樣式探針：DOM 快照抓不到 CSS 的迴歸，而色票與版面正是會被搬進共用檔的東西。
# 三種模式都量——色票在原始碼裡是三個獨立區塊（亮、data-theme、prefers-color-scheme）。
_TOKENS = ["--page", "--surface-1", "--text-1", "--text-2", "--muted",
           "--border", "--grid", "--baseline", "--other",
           "--s1", "--s2", "--s3", "--s4", "--s5", "--s6",
           "--warning", "--serious", "--event", "--up-good"]

_PROBE_SELS = ["body", "header h1", ".sub", ".tiles", ".tile", ".card", ".desc",
               ".note", ".scrollx", "table", "table th", "td.num", ".wrap",
               "button.theme", "#stat", "button.chip", "i.swatch", ".empty",
               ".trend", ".trend .head", ".trend .head .tot"]

_PROBE_PROPS = ["color", "background-color", "font-size", "font-weight",
                "margin-top", "margin-bottom", "padding-top", "border-radius",
                "border-top-width", "display"]

_PROBE_JS = r"""
([tokens, sels, props]) => {
  const out = [];
  const rs = getComputedStyle(document.documentElement);
  out.push("@tokens " + tokens
    .map(t => t + "=" + (rs.getPropertyValue(t).trim() || "—")).join(" "));
  for (const sel of sels) {
    const e = document.querySelector(sel);
    if (!e) { out.push("@style " + sel + "  (無此元素)"); continue; }
    const cs = getComputedStyle(e);
    out.push("@style " + sel + "  " +
      props.map(p => p + "=" + cs.getPropertyValue(p)).join(" "));
  }
  return out.join("\n");
}
"""


def _style_probe(page) -> str:
    args = [_TOKENS, _PROBE_SELS, _PROBE_PROPS]
    blocks = ["── 亮色", page.evaluate(_PROBE_JS, args)]
    page.evaluate("document.documentElement.dataset.theme = 'dark'")
    blocks += ["── 深色（data-theme）", page.evaluate(_PROBE_JS, args)]
    page.evaluate("delete document.documentElement.dataset.theme")
    page.emulate_media(color_scheme="dark")
    blocks += ["── 深色（prefers-color-scheme）", page.evaluate(_PROBE_JS, args)]
    page.emulate_media(color_scheme="light")
    return "\n".join(blocks)


def _digest(page, page_name: str) -> str:
    import re

    raw = page.evaluate(_DIGEST_JS, PAGE_ROOTS[page_name])
    # 儀表板的資料鮮度用牆上時鐘算天數（dashboard.html 的 staleDays），
    # 是五頁唯一會讓快照每天變動的東西
    dom = re.sub(r"已 \d+ 天", "已 N 天", raw)
    # 樣式探針會改 data-theme 與模擬媒體，所以一定排在 DOM 快照之後
    return dom + "\n\n" + _style_probe(page)


def _check_golden(name: str, digest: str) -> None:
    path = GOLDEN_DIR / f"{name}.txt"
    if UPDATE_GOLDEN:
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(digest + "\n", encoding="utf-8")
        print(f"  ↻ 重生 {path.name}（{len(digest.splitlines())} 行）")
        return
    assert path.exists(), (
        f"缺少快照 {path}——第一次建立請跑："
        f"python -X utf8 tests/test_twcrawl.py --update-golden")
    want = path.read_text(encoding="utf-8").rstrip("\n")
    if want != digest:
        got_lines, want_lines = digest.splitlines(), want.splitlines()
        diff = next(
            (f"第 {i + 1} 行\n    快照：{w!r}\n    實際：{g!r}"
             for i, (w, g) in enumerate(zip(want_lines, got_lines)) if w != g),
            f"行數：快照 {len(want_lines)}、實際 {len(got_lines)}")
        raise AssertionError(
            f"{name} 的畫面與快照不符——\n  {diff}\n"
            "  確認是有意的改動之後，跑 --update-golden 重生並審閱 diff。")


def test_pages_render_and_match_golden():
    """五頁（含深連結變體）以合成 payload 渲染：零錯誤、結構與快照一致。"""
    from urllib.parse import quote

    from twcrawl.workspace import Workspace

    cases = [
        ("dashboard", "dashboard.html", ""),
        ("query", "query.html", ""),
        ("query-fixed", "query.html", "?view=fixed"),
        ("query-cat", "query.html", "?cat=" + quote("手搖飲")),
        ("fda", "fda.html", "?src=csm_news"),
        ("year", "year.html", ""),
        ("map", "map.html", ""),
    ]
    with TemporaryDirectory() as td:
        ws = Workspace(Path(td))
        out = _stage_pages(ws, a_payload())
        with browser_context(session_file=None, headed=False) as ctx:
            _stub_tiles(ctx)
            for name, page_name, query in cases:
                page, errs = _open(ctx, out, page_name, query)
                assert not errs, f"{name} 有 JS 錯誤：{errs}"
                digest = _digest(page, page_name)
                assert digest.strip(), f"{name} 什麼都沒渲染"
                _check_golden(name, digest)
                page.close()
    print("✓ 五頁以合成 payload 渲染：零 JS 錯誤、結構快照一致（含深連結變體）")


def test_pages_survive_hostile_and_edge_payloads():
    """店家名含標記不得注入 DOM；殘缺的 payload 不得讓整頁空白。"""
    from twcrawl.workspace import Workspace

    # 自訂元素：不會渲染出任何東西，但沒跳脫的話 querySelector 找得到
    xss = '<twcrawl-xss></twcrawl-xss>"'
    hostile = _replace_strings(
        a_payload(), {"珍奶測試店": "珍奶" + xss, "手搖飲": "手搖" + xss,
                      "其他綜合零售": "零售" + xss, "六獎": "六獎" + xss,
                      "INVOICE0099X": "狀態" + xss})

    lot = a_payload()["lottery"]
    edges = [
        # 缺陷：drawDate 為 null 時 slice() 會 throw，而 throw 發生在
        # app.innerHTML = "" 之後——畫面與「找不到 data.js」一模一樣
        ("drawDate 缺漏",
         a_payload(lottery={**lot, "periods": [],
                            "next": {"label": "x", "drawDate": None,
                                     "pending": 3}})),
        ("沒有比對報告",
         a_payload(fda={"rows": 0, "matches": None, "sources": []})),
        ("舊版 data.js（發票沒有品項）",
         a_payload(invoices=[{k: v for k, v in inv.items() if k != "items"}
                             for inv in a_payload()["invoices"]])),
    ]

    with TemporaryDirectory() as td:
        ws = Workspace(Path(td))
        with browser_context(session_file=None, headed=False) as ctx:
            _stub_tiles(ctx)

            out = _stage_pages(ws, hostile)
            for page_name in PAGE_ROOTS:
                page, errs = _open(ctx, out, page_name)
                assert not errs, f"{page_name} 吃到惡意字串就出錯：{errs}"
                # 儀表板多數跳脫點在 tooltip、查詢頁的狀態行與品項表在展開
                # 列——都要派事件才會渲染
                page.evaluate("""() => {
                  for (const r of document.querySelectorAll(
                      "svg rect[fill='transparent']"))
                    r.dispatchEvent(new MouseEvent("mousemove",
                      {clientX: 20, clientY: 20, bubbles: true}));
                  for (const tr of document.querySelectorAll("tr.inv"))
                    tr.click();
                  // 地圖 marker 的 tooltip 是 hover 才進 DOM（Leaflet 字串
                  // tooltip 走 innerHTML），要派 mouseover 才驗得到跳脫
                  for (const p of document.querySelectorAll(
                      "#map path.leaflet-interactive"))
                    p.dispatchEvent(new MouseEvent("mouseover",
                      {bubbles: true}));
                }""")
                n = page.evaluate(
                    "document.querySelectorAll('twcrawl-xss').length")
                assert n == 0, (
                    f"{page_name} 把店家／分類名當 HTML 執行了（{n} 個節點）"
                    "——payload 字串進 innerHTML 前一律要經 esc")
                page.close()

            for label, payload in edges:
                out = _stage_pages(ws, payload)
                for page_name in PAGE_ROOTS:
                    page, errs = _open(ctx, out, page_name)
                    assert not errs, f"{page_name}／{label}：{errs}"
                    filled = page.evaluate(
                        "!!document.querySelector('#app, #legend')"
                        " && document.body.innerText.trim().length > 0")
                    assert filled, f"{page_name}／{label}：整頁空白"
                    page.close()
    print("✓ 五頁：惡意字串不進 DOM、殘缺 payload 不讓整頁空白")


def test_pages_color_by_slot_not_rank():
    """頁面取色跟 slot 走、不跟金額排名走（issue #10 驗收 3）。

    golden 的 fixture slot 恰與排名重合（首次指派＝排名序），擋不住
    「ui.js 回歸成當期排名取前六」——這裡把 slot 與排名刻意錯開，直接
    斷言 dashboard 與 map 圖例 swatch 的實際顏色落在指派的槽色上。
    """
    from twcrawl.workspace import Workspace

    permuted = {"電信": 3, "超市": 1, "手搖飲": 6, "未分類": None}
    payload = a_payload(categories=[{**c, "slot": permuted[c["name"]]}
                                    for c in a_payload()["categories"]])

    js = """() => {
      const norm = c => { const d = document.createElement("i");
        d.style.color = c; document.body.appendChild(d);
        const v = getComputedStyle(d).color; d.remove(); return v; };
      const slot = n => norm(getComputedStyle(document.documentElement)
        .getPropertyValue("--s" + n).trim());
      const got = {};
      for (const b of document.querySelectorAll(
          ".legend button.leg, #legend button.leg")) {
        const sw = b.querySelector("i.swatch");
        if (sw) got[b.textContent.trim()] =
          norm(getComputedStyle(sw).backgroundColor);
      }
      return { got, s1: slot(1), s3: slot(3), s6: slot(6) };
    }"""

    # 年度頁的佔比條是 inline 取色（.brow .fill），DOM 快照拍不到，一起驗
    js_year = """() => {
      const norm = c => { const d = document.createElement("i");
        d.style.color = c; document.body.appendChild(d);
        const v = getComputedStyle(d).color; d.remove(); return v; };
      const slot = n => norm(getComputedStyle(document.documentElement)
        .getPropertyValue("--s" + n).trim());
      const got = {};
      for (const row of document.querySelectorAll(".brow")) {
        got[row.querySelector(".name").textContent.trim()] =
          norm(getComputedStyle(row.querySelector(".fill")).backgroundColor);
      }
      return { got, s1: slot(1), s3: slot(3), s6: slot(6) };
    }"""

    with TemporaryDirectory() as td:
        out = _stage_pages(Workspace(Path(td)), payload)
        with browser_context(session_file=None, headed=False) as ctx:
            _stub_tiles(ctx)
            for page_name, probe in (("dashboard.html", js), ("map.html", js),
                                     ("year.html", js_year)):
                page, errs = _open(ctx, out, page_name)
                assert not errs, f"{page_name}：{errs}"
                r = page.evaluate(probe)
                for name, key in (("電信", "s3"), ("超市", "s1"),
                                  ("手搖飲", "s6")):
                    assert r["got"].get(name) == r[key], (
                        f"{page_name} 的「{name}」swatch 應取指派的槽色 "
                        f"--{key}，實得 {r['got'].get(name)!r}"
                        "——取色不得回到金額排名")
                page.close()
    print("✓ 取色跟槽位走：slot 與排名錯開時，dashboard／map／year 照指派上色")


def test_dashboard_trend_hover_tooltip():
    """分類趨勢圖 hover 顯示月份・分類・金額（issue #11 驗收）。

    golden 只拍得到 SVG 的元素統計，tooltip 是事件驅動的——這裡對「超市」
    那格的第 3 個月（2026-05，金額 200）派 mousemove，直接驗 tooltip 內容。
    """
    from twcrawl.workspace import Workspace

    js = """() => {
      const cell = [...document.querySelectorAll(".trend .cell")]
        .find(c => c.querySelector(".head").textContent.includes("超市"));
      if (!cell) return { err: "趨勢圖沒有「超市」那格" };
      const hits = cell.querySelectorAll("svg rect[fill='transparent']");
      if (hits.length < 3) return { err: "月份 hit 區塊不足：" + hits.length };
      const r = hits[2].getBoundingClientRect();
      hits[2].dispatchEvent(new MouseEvent("mousemove",
        { clientX: r.x + r.width / 2, clientY: r.y + r.height / 2,
          bubbles: true }));
      return { tip: document.getElementById("tooltip").innerText };
    }"""
    with TemporaryDirectory() as td:
        out = _stage_pages(Workspace(Path(td)), a_payload())
        with browser_context(session_file=None, headed=False) as ctx:
            _stub_tiles(ctx)
            page, errs = _open(ctx, out, "dashboard.html")
            assert not errs, f"dashboard：{errs}"
            r = page.evaluate(js)
            assert "err" not in r, f"趨勢圖結構不對：{r['err']}"
            for want in ("2026-05", "超市", "200"):
                assert want in r["tip"], (
                    f"趨勢圖 tooltip 少了「{want}」——實得：{r['tip']!r}")
            page.close()

            # 單月資料庫沒有「走向」可言（x 會除以零）：整卡不出現、頁面照常
            single = a_payload(months=a_payload()["months"][:1])
            out2 = _stage_pages(Workspace(Path(td)), single)
            page, errs = _open(ctx, out2, "dashboard.html")
            assert not errs, f"dashboard（單月）：{errs}"
            n = page.evaluate("document.querySelectorAll('.trend').length")
            assert n == 0, "單月資料庫不該畫趨勢卡（沒有走向、x 會除以零）"
            assert page.evaluate(
                "document.body.innerText.includes('每月支出')"), \
                "單月資料庫的儀表板不該整頁空白"
            page.close()
    print("✓ 分類趨勢圖：hover tooltip 顯示月份・分類・金額；單月不畫趨勢卡")


def test_dashboard_budget_tile():
    """預算磚（issue #12）：有設定才出現、對照現行預算、兩者可只設其一。

    fixture：6月 360／總額 800（45%、剩 440）、3月 849 與 5月 899 超總額；
    非必要 4月/6月各 60、上限 50（本月超 10、兩個月破上限）。
    """
    from twcrawl.workspace import Workspace

    js = """() => {
      const t = [...document.querySelectorAll(".tile")]
        .find(x => x.textContent.includes("預算") ||
                   x.textContent.includes("上限已用"));
      return t ? { label: t.querySelector(".label").textContent,
                   value: t.querySelector(".value").textContent,
                   text: t.innerText } : null;
    }"""
    variants = [
        ("雙預算", a_payload(), "本月預算已用（6月）", "45%",
         ["剩 NT$440", "超總額：3月、5月",
          "非必要 NT$60／上限 NT$50（超 NT$10）", "破上限：4月、6月"]),
        # 沒設總額時標籤要講清楚 120% 是「非必要上限」的比值，不是總額超支
        ("只設上限", a_payload(budget={"monthly": None, "unnecessary": 50.0}),
         "本月非必要上限已用（6月）", "120%",
         ["非必要 NT$60／上限 NT$50（超 NT$10）", "破上限：4月、6月"]),
        ("無設定", a_payload(budget=None), None, None, []),
    ]
    with TemporaryDirectory() as td:
        ws = Workspace(Path(td))
        with browser_context(session_file=None, headed=False) as ctx:
            _stub_tiles(ctx)
            for name, payload, label, value, wants in variants:
                out = _stage_pages(ws, payload)
                page, errs = _open(ctx, out, "dashboard.html")
                assert not errs, f"dashboard（{name}）：{errs}"
                r = page.evaluate(js)
                if value is None:
                    assert r is None, f"{name}：沒設定不該有預算磚——{r}"
                else:
                    assert r is not None, f"{name}：預算磚沒出現"
                    assert r["label"] == label, (
                        f"{name}：磚標籤該是「{label}」，實得 {r['label']!r}")
                    assert r["value"] == value, (
                        f"{name}：磚值該是 {value}，實得 {r['value']!r}")
                    for want in wants:
                        assert want in r["text"], (
                            f"{name}：磚上少了「{want}」——實得 {r['text']!r}")
                page.close()
    print("✓ 預算磚：雙預算 45%＋失守月份點名、只設上限 120%、沒設定就沒磚")


def test_query_status_display():
    """發票狀態顯示（issue #14）：常態不佔列上版面、非常態標原始碼、
    展開明細一律有中文狀態行。golden 不點列，這裡直接驗 DOM。"""
    from twcrawl.workspace import Workspace

    js = """() => {
      const rows = [...document.querySelectorAll("tr.inv")];
      const byNum = t => rows.find(r => r.textContent.includes(t));
      const aa1 = byNum("AA1"), aa7 = byNum("AA7");
      aa1.click();
      const detail = aa1.nextElementSibling.textContent;
      return {
        aa1Badge: !!aa1.querySelector(".st-mark"),
        aa7Badge: (aa7.querySelector(".st-mark") || {}).textContent || null,
        detail,
      };
    }"""
    with TemporaryDirectory() as td:
        out = _stage_pages(Workspace(Path(td)), a_payload())
        with browser_context(session_file=None, headed=False) as ctx:
            _stub_tiles(ctx)
            page, errs = _open(ctx, out, "query.html")
            assert not errs, f"query：{errs}"
            r = page.evaluate(js)
            assert not r["aa1Badge"], "常態「開立」不該在列上長徽章"
            assert r["aa7Badge"] == "（INVOICE0099X）", (
                f"未收錄碼要在列上原樣標註，實得 {r['aa7Badge']!r}")
            assert "發票狀態：開立" in r["detail"], (
                f"展開明細要有中文狀態行，實得 {r['detail']!r}")
            page.close()
    print("✓ 查詢頁狀態顯示：開立不標、未收錄碼原樣標、展開有中文狀態行")


def test_map_seller_search():
    """地圖店家搜尋（issue #15）：輸入即過濾圓點、與圖例/時間取交集、
    清空恢復。fixture 兩個有座標的店家（超市、珍奶）。"""
    from twcrawl.workspace import Workspace

    js = """(step) => {
      const sfind = document.getElementById("sfind");
      const count = () =>
        document.querySelectorAll("#map path.leaflet-interactive").length;
      const type = v => { sfind.value = v;
        sfind.dispatchEvent(new Event("input", { bubbles: true })); };
      const legBtn = () => [...document.querySelectorAll("#legend button.leg")]
        .find(x => x.textContent.includes("手搖"));
      if (step === "baseline") return count();
      if (step === "search") { type("超市"); return count(); }
      if (step === "clear") { type(""); return count(); }
      if (step === "legend") { type("珍奶"); legBtn().click(); return count(); }
      if (step === "time") {
        legBtn().click();                       // 恢復圖例，只留時間交集
        document.getElementById("mFrom").value = "2026-03";
        const mTo = document.getElementById("mTo");
        mTo.value = "2026-03";
        mTo.dispatchEvent(new Event("change", { bubbles: true }));
        return count();
      }
      if (step === "hostile") {   // 搜尋框輸入惡意字串：只比對、不進 DOM
        type('<twcrawl-xss></twcrawl-xss>"');
        return count() +
          document.querySelectorAll("twcrawl-xss").length * 100;
      }
    }"""
    with TemporaryDirectory() as td:
        out = _stage_pages(Workspace(Path(td)), a_payload())
        with browser_context(session_file=None, headed=False) as ctx:
            _stub_tiles(ctx)
            page, errs = _open(ctx, out, "map.html")
            assert not errs, f"map：{errs}"
            for step, want, why in (
                    ("baseline", 2, "預設近三月應有 2 個圓點"),
                    ("search", 1, "搜「超市」應剩 1 點"),
                    ("clear", 2, "清空搜尋應回復 2 點"),
                    ("legend", 0, "搜「珍奶」∩圖例隱藏手搖飲應為 0"),
                    ("time", 0, "搜「珍奶」∩區間 2026-03 應為 0（該月無其發票）"),
                    ("hostile", 0, "搜尋字串不得進 DOM（xss 計 100/個）")):
                got = page.evaluate(js, step)
                assert got == want, f"{why}，實得 {got}"
                if step == "search":
                    stat = page.evaluate(
                        "document.getElementById('stat').textContent")
                    assert "1 家（NT$500）" in stat, (
                        "stat 的家數與金額要一起跟著過濾走（超市 4-6月 "
                        f"200+300=500），實得 {stat!r}")
            page.close()
    print("✓ 地圖店家搜尋：輸入過濾、與圖例/時間交集、清空恢復")


def test_payload_contract():
    """a_payload() 的形狀必須跟 export.build_payload 一致，手打的 fixture 才漂不掉。"""
    from twcrawl import export, lottery
    from twcrawl.categories import Classifier
    from twcrawl.workspace import Workspace

    # 以領域值為鍵的對照表（命中層級、分類名），鍵隨資料變動不是形狀的一部分
    value_keyed = {"months[].byCategory"}

    def shape(v, path=""):
        """{路徑} 集合：dict 收鍵、list 取首元素往下走。"""
        out: set[str] = set()
        if path in value_keyed:
            return out
        if isinstance(v, dict):
            for k, sub in v.items():
                p = f"{path}.{k}" if path else k
                out.add(p)
                out |= shape(sub, p)
        elif isinstance(v, list):
            # 聯集而非只看首元素：某張發票沒有品項，不該讓 items 的欄位隱形
            for x in v:
                out |= shape(x, path + "[]")
        return out

    with TemporaryDirectory() as td:
        td = Path(td)
        ws = Workspace(td)
        ws.ensure_out()
        ws.match_report.write_text(
            "﻿level,inv_num,inv_date,invoice_side,fda_side,source\n"
            "店家,AA2,2026-03-12,測試超市,測試超市股份有限公司,edible_oil\n",
            encoding="utf-8")
        # 有設定 → payload 有 budget 鍵，形狀才對得上 a_payload
        ws.budget.write_text('{"monthly": 800, "unnecessary": 50}',
                             encoding="utf-8")
        conn = db.connect(ws.db)
        try:
            # 四個月、電信 599×3 → 固定支出；未分類店家 → uncategorized；
            # 手搖飲 → unnecessary；中獎號碼 → lottery.periods 與 next
            db.upsert_invoices(conn, [
                an_invoice("AA1", "2026-03-05", "測試電信", 599.0),
                an_invoice("AA2", "2026-03-12", "測試超市", 250.0),
                an_invoice("AA3", "2026-04-05", "測試電信", 599.0),
                an_invoice("AB12345678", "2026-04-18", "五十嵐測試店", 60.0),
                an_invoice("AA5", "2026-05-05", "測試電信", 599.0),
                an_invoice("AA7", "2026-05-12", "神秘測試舖", 100.0),
            ])
            db.upsert_items(conn, [
                an_item("AA2", 1, "雞蛋", 250.0, quantity=1, unit_price=250.0),
            ])
            db.upsert_lottery_draws(conn, [
                {"period": "11503", "special": "11223344", "grand": "55667788",
                 "first": ["12345678"], "extra": ["217"],
                 "claim_start": "2026-06-06", "claim_end": "2026-09-05"},
            ])
            real = export.build_payload(conn, ws, Classifier(ws.rules))
        finally:
            conn.close()  # Windows：先關連線才能清 TemporaryDirectory

    fake = a_payload()
    assert set(real) == set(fake), (
        f"頂層鍵漂了——build_payload 多了 {set(real) - set(fake)}、"
        f"少了 {set(fake) - set(real)}")

    # 只比對兩邊都非空的部分；空的照實印出來，不假裝比過了
    skipped = []
    for key in sorted(real):
        if isinstance(real[key], list) and not real[key]:
            skipped.append(key)
            continue
        want, got = shape(fake[key], key), shape(real[key], key)
        assert want == got, (
            f"{key} 的形狀漂了——build_payload 多了 {sorted(got - want)}、"
            f"少了 {sorted(want - got)}")
    assert lottery.period_label("11503") == "115年03-04月", \
        "a_payload 的期別標籤是照 lottery.period_label 手寫的"
    note = f"（未比對空清單：{', '.join(skipped)}）" if skipped else ""
    print(f"✓ 合成 payload 與 build_payload 形狀一致{note}")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        # SystemExit 是這個 codebase 的正常錯誤通道（categories/backup/fda/
        # bizreg/handoff/browser 都用它）。只抓 Exception 的話，一個逃出的
        # SystemExit 會靜默中止整輪、總數行永遠不印——看起來像「全過」。
        except BaseException as e:
            failed += 1
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
            if isinstance(e, KeyboardInterrupt):
                raise
    print(f"\n{len(tests) - failed}/{len(tests)} 通過")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
