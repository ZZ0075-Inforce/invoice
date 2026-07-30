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


# ------------------------------------------------------------- 測試項目 --

def test_table_extraction_and_pagination():
    srv, url = _serve()
    try:
        with browser_context(session=None, headed=False) as ctx:
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
        with browser_context(session=None, headed=False) as ctx:
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
                with browser_context(session=None, headed=False) as ctx:
                    res = fda.fetch(ctx.new_page(), conn, url=url, max_pages=10,
                                    out_dir=Path(d) / "out")
                assert res["rows"] == PAGES * PER_PAGE, res
                n = conn.execute("SELECT COUNT(*) FROM fda_rows").fetchone()[0]
                assert n == PAGES * PER_PAGE, n

                # 重跑一次不應新增列（僅更新 last_seen）
                with browser_context(session=None, headed=False) as ctx:
                    fda.fetch(ctx.new_page(), conn, url=url, max_pages=10,
                              out_dir=Path(d) / "out")
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
                with browser_context(session=None, headed=False) as ctx:
                    res = fda.fetch(ctx.new_page(), conn, url=url, max_pages=10,
                                    out_dir=Path(d) / "out")
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
    import os

    from twcrawl.netcapture import Capture

    srv, url = _serve()
    cwd = os.getcwd()
    try:
        with TemporaryDirectory() as d:
            os.chdir(d)  # Capture 寫到相對路徑 captures/
            try:
                cap = Capture("test")
                with browser_context(session=None, headed=False) as ctx:
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
                os.chdir(cwd)  # 必須先離開暫存目錄，Windows 才能清理
    finally:
        srv.shutdown()


def test_wait_for_operator_pumps_events():
    """等待人工操作期間發生的 XHR 必須被錄到（回歸：卡在 input()/sleep 時事件不派發）。"""
    import os
    import threading

    from twcrawl.browser import wait_for_operator
    from twcrawl.netcapture import Capture

    srv, url = _serve()
    cwd = os.getcwd()
    try:
        with TemporaryDirectory() as d:
            os.chdir(d)
            try:
                flag = Path(d) / "done.flag"
                os.environ["TWCRAWL_DONE_FILE"] = str(flag)
                cap = Capture("pump")
                with browser_context(session=None, headed=False) as ctx:
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
                os.chdir(cwd)
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

    with TemporaryDirectory() as d:
        conn = db.connect(Path(d) / "m.sqlite")
        try:
            db.upsert_invoices(conn, [
                {"inv_num": "AA11111111", "inv_date": "2026-05-01",
                 "seller_name": "測試商行一店", "amount": 100.0,
                 "source": "t", "raw": "{}"},   # 未分類（零售視角）→ 品項參與比對
                {"inv_num": "BB22222222", "inv_date": "2026-01-01",  # since 之前，應排除
                 "seller_name": "測試商行", "amount": 50.0,
                 "source": "t", "raw": "{}"},
                {"inv_num": "CC33333333", "inv_date": "2026-05-02",
                 "seller_name": "巷口快炒小吃店", "amount": 120.0,  # 餐飲現調 → 濾品項
                 "source": "t", "raw": "{}"},
                {"inv_num": "DD44444444", "inv_date": "2026-05-03",
                 "seller_name": "測試商行二店", "amount": 80.0,
                 "source": "t", "raw": "{}"},   # 上榜通路但品項無交集 → 排除
                {"inv_num": "EE55555555", "inv_date": "2026-05-04",
                 "seller_name": "測試商行三店", "amount": 90.0,
                 "source": "t", "raw": "{}"},   # 上榜通路且無明細 → 保留＋註記
            ])
            db.upsert_items(conn, [
                {"inv_num": "AA11111111", "row_no": 1, "description": "特級沙拉油",
                 "amount": 100.0, "raw": "[]"},
                {"inv_num": "AA11111111", "row_no": 2, "description": "230",  # 純數字
                 "amount": 230.0, "raw": "[]"},
                {"inv_num": "AA11111111", "row_no": 3, "description": "苦茶油",
                 "amount": 500.0, "raw": "[]"},
                {"inv_num": "CC33333333", "row_no": 1, "description": "蝦仁蛋炒飯",
                 "amount": 120.0, "raw": "[]"},   # 快炒店菜名撞即食包品名
                {"inv_num": "DD44444444", "row_no": 1, "description": "抽取式衛生紙",
                 "amount": 80.0, "raw": "[]"},    # 與該業者下架產品無交集
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
            res = run_match(conn, since="2026-03-01", out_dir=Path(d) / "out",
                            classifier=Classifier(local_path=Path(d) / "no.json"))
            # 店家層級：AA 品項有交集保留、EE 無明細保留；BB 在 since 前、
            # DD 品項皆不在該業者下架清單 → 排除
            assert res["seller_hits"] == 2, res
            assert res["seller_clears"] == 1, res
            assert res["prod_hits"] == 1, res     # 沙拉油命中、230 純數字排除
            assert res["news_hits"] == 1, res     # 苦茶油命中警訊標題
            assert res["eatery_skips"] == 1, res  # 小吃店炒飯×即食包＝濾除
            report = Path(d) / "out" / "match_report.csv"
            assert report.exists(), "應輸出報告 CSV"
            import csv as _csv
            with report.open(encoding="utf-8-sig") as f:
                rows = list(_csv.DictReader(f))
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
            rows = [{"inv_num": "AB12345678", "inv_date": "2024-05-03",
                     "seller_name": "測試", "amount": 100.0, "source": "t", "raw": "{}"}]
            db.upsert_invoices(conn, rows)
            db.upsert_invoices(conn, rows)
            n = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
            assert n == 1, f"重複匯入應維持 1 筆，實得 {n}"

            items = [{"inv_num": "AB12345678", "row_no": 1, "description": "x",
                      "amount": 100.0, "raw": "[]"}]
            db.upsert_items(conn, items)
            db.upsert_items(conn, items)
            n = conn.execute("SELECT COUNT(*) FROM invoice_items").fetchone()[0]
            assert n == 1, f"明細重複匯入應維持 1 筆，實得 {n}"
            print("✓ SQLite upsert 具冪等性")
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

    with TemporaryDirectory() as td:
        td = Path(td)
        conn = db.connect(td / "t.sqlite")
        try:
            db.upsert_invoices(conn, [
                {"inv_num": "AA1", "inv_date": "2026-05-03",
                 "seller_name": "五十嵐測試店", "amount": 60.0},
                {"inv_num": "AA2", "inv_date": "2026-05-10",
                 "seller_name": "全聯實業股份有限公司", "amount": 200.0},
                {"inv_num": "AA3", "inv_date": "2026-05-12",
                 "seller_name": "神秘小舖", "amount": 100.0},
                {"inv_num": "AA4", "inv_date": "2026-06-01",
                 "seller_name": "全聯實業股份有限公司", "amount": 300.0},
            ])
            cl = Classifier(local_path=td / "沒有這個檔.json")
            payload = export.build_payload(conn, cl)
            assert [m["month"] for m in payload["months"]] == ["2026-05", "2026-06"]
            assert payload["months"][0]["total"] == 360.0
            assert payload["categories"][0]["name"] == "超市", "分類應依金額排序"
            assert len(payload["unnecessary"]) == 1
            assert payload["unnecessary"][0]["amount"] == 60.0
            assert any(s["name"] == "神秘小舖" for s in payload["uncategorized"])

            dash = export.write_export(conn, out_dir=td / "out", classifier=cl)
            assert dash.exists()
            data_js = (td / "out" / "data.js").read_text(encoding="utf-8")
            assert data_js.startswith("window.TWCRAWL_DATA = ")
        finally:
            conn.close()
    print("✓ export 衍生儀表板資料與模板就位")


def test_item_override_invoice_level():
    from twcrawl import export
    from twcrawl.categories import Classifier

    with TemporaryDirectory() as td:
        td = Path(td)
        cl = Classifier(local_path=td / "沒有這個檔.json")
        cos = "好市多股份有限公司"
        assert cl.for_invoice(cos, ["95無鉛汽油"]).name == "加油"
        assert cl.for_invoice(cos, ["九二無鉛汽油", "礦泉水"]).name == "加油"
        assert cl.for_invoice(cos, ["鮮奶", None, ""]).name == "量販", \
            "沒命中品項規則就落回店家分類"

        local = td / "categories.local.json"
        local.write_text(json.dumps({"item_rules": {"無鉛汽油": "個人品項"}},
                                    ensure_ascii=False), encoding="utf-8")
        assert Classifier(local_path=local).for_invoice(
            cos, ["95無鉛汽油"]).name == "個人品項", "品項規則個人層應優先"

        # 接線：發票層級要進 invoices／months，店家層級要進 sellers。
        # 寫反了純測試看不出來，所以這裡留三條斷言。
        conn = db.connect(td / "t.sqlite")
        try:
            db.upsert_invoices(conn, [
                {"inv_num": "CS1", "inv_date": "2026-02-18",
                 "seller_name": "好市多股份有限公司",
                 "amount": 1200.0},
                {"inv_num": "CS2", "inv_date": "2026-02-20",
                 "seller_name": "好市多股份有限公司",
                 "amount": 500.0},
            ])
            db.upsert_items(conn, [
                {"inv_num": "CS1", "row_no": 1, "description": "95無鉛汽油",
                 "amount": 1200.0, "raw": "[]"},
                {"inv_num": "CS2", "row_no": 1, "description": "鮮奶",
                 "amount": 500.0, "raw": "[]"},
            ])
            payload = export.build_payload(conn, cl)
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

    with TemporaryDirectory() as td:
        td = Path(td)
        (td / "out").mkdir()
        (td / "captures" / "a").mkdir(parents=True)
        dbf = td / "out" / "t.sqlite"
        dbf.write_bytes(b"sqlite-bytes")
        (td / "captures" / "a" / "r.json").write_text("{}", encoding="utf-8")

        out = bk.make_backup("pw123", db_path=dbf,
                             captures_dir=td / "captures", out_dir=td / "bak")
        with pyzipper.AESZipFile(out) as zf:
            zf.setpassword(b"pw123")
            names = zf.namelist()
            assert any(n.endswith("t.sqlite") for n in names)
            got = zf.read(next(n for n in names if n.endswith("r.json")))
            assert got == b"{}", "解密內容應與原檔一致"

        wrong_ok = False
        try:
            with pyzipper.AESZipFile(out) as zf:
                zf.setpassword(b"wrong")
                zf.read(names[0])
            wrong_ok = True
        except Exception:
            pass
        assert not wrong_ok, "錯誤密碼不應能解密"
    print("✓ backup 加密備份可往返、錯誤密碼被拒")


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
                {"inv_num": "BB1", "inv_date": "2026-05-01",
                 "seller_name": "拾光咖啡有限公司", "seller_ban": "12345678",
                 "amount": 100.0},
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

    with TemporaryDirectory() as td:
        td = Path(td)
        local = td / "categories.local.json"
        local.write_text(json.dumps(
            {"aliases": {"神秘": "MYSTERY LAB"}}, ensure_ascii=False),
            encoding="utf-8")
        conn = db.connect(td / "t.sqlite")
        try:
            db.upsert_invoices(conn, [
                {"inv_num": "CC1", "inv_date": "2026-05-01",
                 "seller_name": "神秘小舖", "seller_ban": "12345678",
                 "amount": 100.0},
            ])
            conn.execute(
                "insert into biz_registry (ban, name, address, industry) "
                "values ('12345678','神秘小舖','台中市測試路1號','餐盒零售')")
            conn.commit()
            payload = export.build_payload(conn, Classifier(local_path=local))
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

    from twcrawl.cli import _auto_month_range

    assert _auto_month_range("2026-06-15", dtm.date(2026, 7, 27)) == ("2026-06", "2026-07")
    assert _auto_month_range(None, dtm.date(2026, 2, 10)) == ("2025-09", "2026-02")
    print("✓ update 自動月份區間（重抓最新月補漏、空庫回推 5 個月）")


def test_export_items_and_query_page():
    from twcrawl import export
    from twcrawl.categories import Classifier

    with TemporaryDirectory() as td:
        td = Path(td)
        conn = db.connect(td / "t.sqlite")
        try:
            db.upsert_invoices(conn, [
                {"inv_num": "DD1", "inv_date": "2026-05-03",
                 "seller_name": "五十嵐測試店", "amount": 60.0,
                 "card_no": "/SECRET99"},
            ])
            db.upsert_items(conn, [
                {"inv_num": "DD1", "row_no": 1, "description": "珍珠鮮奶茶",
                 "quantity": 1, "unit_price": 60.0, "amount": 60.0},
            ])
            cl = Classifier(local_path=td / "沒有這個檔.json")
            payload = export.build_payload(conn, cl)
            row = payload["invoices"][0]
            assert row["num"] == "DD1", "發票列應帶發票號碼（查詢頁對帳用）"
            assert row["items"][0]["desc"] == "珍珠鮮奶茶"
            assert row["items"][0]["price"] == 60.0
            assert "SECRET99" not in json.dumps(payload, ensure_ascii=False), \
                "載具號碼永不進 data.js（ADR-0002）"

            export.write_export(conn, out_dir=td / "out", classifier=cl)
            assert (td / "out" / "query.html").exists(), "export 應就位查詢頁"
            assert (td / "out" / "fda.html").exists(), "export 應就位食安頁"
        finally:
            conn.close()
    print("✓ export：品項與發票號碼進 data.js、查詢頁就位、載具號碼排除")


def test_serve_rules_writeback():
    import threading
    import urllib.request

    from twcrawl import export
    from twcrawl import serve as serve_mod
    from twcrawl.categories import Classifier

    with TemporaryDirectory() as td:
        td = Path(td)
        dbf = td / "t.sqlite"
        conn = db.connect(dbf)
        try:
            db.upsert_invoices(conn, [
                {"inv_num": "EE1", "inv_date": "2026-05-01",
                 "seller_name": "神祕測試店", "amount": 100.0},
            ])
            local = td / "categories.local.json"
            local.write_text(json.dumps(
                {"aliases": {"神祕": "MYSTERY"},
                 "item_rules": {"無鉛汽油": "加油"}},
                ensure_ascii=False), encoding="utf-8")
            out = td / "out"
            export.write_export(conn, out_dir=out,
                                classifier=Classifier(local_path=local))
        finally:
            conn.close()

        httpd = serve_mod.make_server(dbf, out_dir=out, port=0, local_path=local)
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


def test_export_match_details():
    from twcrawl import export
    from twcrawl.categories import Classifier

    with TemporaryDirectory() as td:
        td = Path(td)
        out = td / "out"
        out.mkdir()
        (out / "match_report.csv").write_text(
            "﻿level,inv_num,inv_date,invoice_side,fda_side,source\n"
            "店家,AA1,2026-05-01,富利餐飲,富利餐飲股份有限公司,edible_oil\n",
            encoding="utf-8")
        conn = db.connect(td / "t.sqlite")
        try:
            db.upsert_invoices(conn, [
                {"inv_num": "AA1", "inv_date": "2026-05-01",
                 "seller_name": "富利餐飲", "amount": 100.0}])
            cl = Classifier(local_path=td / "沒有.json")
            payload = export.build_payload(
                conn, cl, match_report=out / "match_report.csv")
            assert payload["fda"]["match"] == {"店家": 1}
            m = payload["fda"]["matches"][0]
            assert m["level"] == "店家" and m["fda"].startswith("富利"), m
            assert m["date"] == "2026-05-01" and m["source"] == "edible_oil"
            assert m["num"] == "AA1", "命中要帶發票號碼（食安頁回鏈查詢頁）"
            src = next(s for s in payload["fda"]["sources"]
                       if s["key"] == "edible_oil")
            assert src["label"] == "中聯油脂案" and src["kind"] == "事件"
            assert src["hits"] == 1, "來源要帶命中數（食安頁總覽用）"

            none = export.build_payload(conn, cl,
                                        match_report=td / "沒有這份報告.csv")
            assert none["fda"]["matches"] is None, "沒報告時不該撿到別處的檔案"
        finally:
            conn.close()
    print("✓ export：match 命中明細進 payload（月報 FDA 卡）、報告路徑跟著 out_dir")


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
                {"inv_num": "AB12345678", "inv_date": "2026-05-10",
                 "seller_name": "頭獎店", "amount": 100},
                {"inv_num": "CD00000217", "inv_date": "2026-06-01",
                 "seller_name": "小七", "amount": 55},
                {"inv_num": "EF99999999", "inv_date": "2026-05-20",
                 "seller_name": "沒中", "amount": 80},
                {"inv_num": "GH11223344", "inv_date": "2026-07-01",
                 "seller_name": "期別外", "amount": 999},
            ])
            r = lottery.check_invoices(conn)
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
                {"inv_num": "AB12345678", "inv_date": "2026-05-10",
                 "seller_name": "兩者皆中", "amount": 100},
                {"inv_num": "CD11112222", "inv_date": "2026-06-02",
                 "seller_name": "雲端八百", "amount": 60},
                {"inv_num": "EF33334444", "inv_date": "2026-05-20",
                 "seller_name": "沒中", "amount": 80},
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


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通過")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
