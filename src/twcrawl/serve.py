"""twcrawl serve — 本機小站：同一套頁面，外加分類規則寫回與控制台。

同頁雙模式（docs/adr/0002）：file:// 開檔唯讀＋複製規則片段；serve 之下頁面
偵測到 http: 協定，未分類清單與店家查詢長出「存檔」，POST /api/rules 把規則
併入 categories.local.json、重跑 export 重生 data.js。只綁 127.0.0.1，不對外。

控制台（issue #20、#21）另外掛四個端點：POST /api/jobs 啟動白名單內的工作、
GET /api/jobs/current 取進度、POST /api/jobs/signal 完成人工交接（登入）、
POST /api/jobs/cancel 中止。工作本身跑在子行程，理由見 `jobs.py`。
"""

from __future__ import annotations

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db as db_mod
from . import export
from . import jobs as jobs_mod
from .categories import Classifier, load_local_config
from .workspace import Workspace


def merge_rules(updates: dict[str, str],
                local_path: Path | str,
                aliases: dict[str, str] | None = None) -> int:
    """把 {店名: 分類}（與選填的 {登記名: 招牌名}）併入個人規則檔，
    保留 unnecessary 等其他欄位。"""
    p = Path(local_path)
    cfg = load_local_config(p)
    if updates:
        rules = dict(cfg.get("rules") or {})
        rules.update(updates)
        cfg["rules"] = rules
    if aliases:
        al = dict(cfg.get("aliases") or {})
        al.update(aliases)
        cfg["aliases"] = al
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return len(updates) + len(aliases or {})


class _Handler(SimpleHTTPRequestHandler):
    # 由 make_server 合成子類別時注入（每請求一個實例，只能走類別屬性）
    ws: Workspace
    runner: jobs_mod.Runner

    def log_message(self, *args):  # GET 靜音；POST 成功時自己印
        pass

    def do_GET(self):
        if self.path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return
        u = urlparse(self.path)
        if u.path == "/api/jobs/current":
            # API 一律擋跨來源，GET 也不例外——工作輸出含使用者資料，
            # 「跨來源讀不到回應」是靠瀏覽器的 CORS 政策，不該是唯一的一道
            if not self._same_origin():
                self._json(403, {"ok": False, "error": "不接受跨來源請求"})
                return
            self._job_status(parse_qs(u.query))
            return
        super().do_GET()

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self) -> bool:
        """擋跨來源請求。

        使用者開著 serve 的時候，另一個分頁的網頁也能對 127.0.0.1 發請求；
        這些 API 會改檔案、會起子行程，不該讓別人按得動。瀏覽器發跨來源
        請求時一定會帶 Origin，沒帶的是同源導覽或非瀏覽器客戶端（curl、
        測試裡的 urllib），照舊放行。
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin.rstrip("/") == f"http://{self.headers.get('Host', '')}"

    def _job_status(self, query: dict) -> None:
        job = self.runner.current()
        if job is None:
            self._json(200, {"ok": True, "job": None})
            return
        try:
            since = int((query.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        self._json(200, {"ok": True, "job": job.snapshot(since=max(0, since))})

    def do_POST(self):
        if not self._same_origin():
            self._json(403, {"ok": False, "error": "不接受跨來源請求"})
            return
        if self.path == "/api/jobs":
            self._start_job()
            return
        if self.path == "/api/jobs/signal":
            self._signal_job()
            return
        if self.path == "/api/jobs/cancel":
            self._cancel_job()
            return
        if self.path != "/api/rules":
            self._json(404, {"ok": False, "error": "未知端點"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            updates = {str(k).strip(): str(v).strip()
                       for k, v in (body.get("set") or {}).items()}
            aliases = {str(k).strip(): str(v).strip()
                       for k, v in (body.get("aliases") or {}).items()}
            if (not updates and not aliases) or any(
                    not k or not v
                    for k, v in [*updates.items(), *aliases.items()]):
                raise ValueError
        except Exception:
            self._json(400, {"ok": False, "error":
                             '需要 {"set": {"店名": "分類"}} 或'
                             ' {"aliases": {"登記名": "招牌名"}}'})
            return
        try:
            merge_rules(updates, self.ws.rules, aliases=aliases)
            conn = db_mod.connect(self.ws.db)  # 每個請求自己開，執行緒安全
            try:
                export.write_export(conn, self.ws, Classifier(self.ws.rules))
            finally:
                conn.close()
        except SystemExit as e:  # 規則檔防呆的人話訊息，轉給頁面顯示
            self._json(500, {"ok": False, "error": str(e)})
            return
        for k, v in updates.items():
            print(f"已歸類：{k} → {v}")
        self._json(200, {"ok": True, "count": len(updates)})

    def _start_job(self) -> None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            name = str(body["cmd"])
            params = body.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError
        except Exception:
            self._json(400, {"ok": False, "error":
                             '需要 {"cmd": "工作名稱", "params": {…}}'})
            return
        try:
            job = self.runner.start(name, self.ws.root, params=params)
        except KeyError:
            self._json(400, {"ok": False, "error":
                             f"不認得的工作：{name}"
                             f"（可用：{'、'.join(jobs_mod.ALLOWED)}）"})
            return
        except jobs_mod.BadParams as e:   # 人話，原樣給頁面顯示
            self._json(400, {"ok": False, "error": str(e)})
            return
        except jobs_mod.Busy as e:
            self._json(409, {"ok": False, "error": str(e)})
            return
        print(f"控制台啟動工作：{job.title}")
        self._json(202, {"ok": True, "job": job.snapshot()})

    def _signal_job(self) -> None:
        """「我已登入」：把訊號檔放下去，等在那裡的子行程就會往下跑。"""
        job = self.runner.current()
        if job is None or not job.signal_operator():
            # 沒在等人工的時候提早放訊號，會讓真正問起時被立刻放行——
            # 所以這裡拒絕，而不是寫個檔案假裝成功
            self._json(409, {"ok": False, "error": "現在沒有在等人工操作"})
            return
        print("控制台：人工交接完成，工作繼續")
        self._json(200, {"ok": True})

    def _cancel_job(self) -> None:
        job = self.runner.current()
        if job is None or not job.cancel():
            self._json(409, {"ok": False, "error": "現在沒有進行中的工作"})
            return
        print(f"控制台中止工作：{job.title}")
        self._json(200, {"ok": True})


def make_server(ws: Workspace, port: int = 8765) -> ThreadingHTTPServer:
    # runner 跟著這台 server 走，不是模組層全域——測試每起一台就有乾淨的一份
    runner = jobs_mod.Runner()
    handler = type("Handler", (_Handler,), {"ws": ws, "runner": runner})
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port), partial(handler, directory=str(ws.ensure_out())))
    httpd.runner = runner   # 收站時要用（見 stop）
    return httpd


def stop(httpd: ThreadingHTTPServer) -> None:
    """收站：先把還在跑的工作收掉，再關 server。

    工作是子行程，serve 結束並不會連帶收掉它——POSIX 上它還自成一個行程群組
    （中止要殺得掉整棵就得如此），連 Ctrl+C 都收不到。不主動中止的話，關掉
    視窗留下的正是 issue #21 要防的半死子行程（含底下的 Chromium），而使用者
    以為自己已經全部停了。
    """
    job = httpd.runner.current()
    if job is not None and job.cancel():
        print(f"（已中止還在跑的工作：{job.title}）")
    httpd.server_close()


def serve(ws: Workspace, port: int = 8765,
          open_browser: bool = True, page: str = "dashboard.html") -> None:
    conn = db_mod.connect(ws.db)  # 起站先重生，保證頁面是最新資料
    try:
        export.write_export(conn, ws, Classifier(ws.rules))
    finally:
        conn.close()
    httpd = make_server(ws, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/{page}"
    print(f"serve 模式：{url}")
    print("頁面上的「存檔」會寫回 categories.local.json 並重生資料；"
          "控制台頁可以直接按更新／抓發票；只綁本機、不對外；Ctrl+C 結束。")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        stop(httpd)
