"""twcrawl serve — 本機小站：同一套頁面，外加分類規則寫回。

同頁雙模式（docs/adr/0002）：file:// 開檔唯讀＋複製規則片段；serve 之下頁面
偵測到 http: 協定，未分類清單與店家查詢長出「存檔」，POST /api/rules 把規則
併入 categories.local.json、重跑 export 重生 data.js。只綁 127.0.0.1，不對外。
"""

from __future__ import annotations

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db as db_mod
from . import export
from .categories import LOCAL_RULES_PATH, Classifier, load_local_config


def merge_rules(updates: dict[str, str],
                local_path: Path | str = LOCAL_RULES_PATH,
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
    db_path: str = ""
    local_path: Path = LOCAL_RULES_PATH
    out_dir: Path = Path("out")

    def log_message(self, *args):  # GET 靜音；POST 成功時自己印
        pass

    def do_GET(self):
        if self.path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return
        super().do_GET()

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
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
            merge_rules(updates, self.local_path, aliases=aliases)
            conn = db_mod.connect(self.db_path)  # 每個請求自己開，執行緒安全
            try:
                export.write_export(
                    conn, out_dir=self.out_dir,
                    classifier=Classifier(local_path=self.local_path))
            finally:
                conn.close()
        except SystemExit as e:  # 規則檔防呆的人話訊息，轉給頁面顯示
            self._json(500, {"ok": False, "error": str(e)})
            return
        for k, v in updates.items():
            print(f"已歸類：{k} → {v}")
        self._json(200, {"ok": True, "count": len(updates)})


def make_server(db_path, out_dir: Path | str = Path("out"), port: int = 8765,
                local_path: Path | str = LOCAL_RULES_PATH) -> ThreadingHTTPServer:
    out_dir = Path(out_dir)
    handler = type("Handler", (_Handler,), {
        "db_path": str(db_path),
        "local_path": Path(local_path),
        "out_dir": out_dir,
    })
    return ThreadingHTTPServer(
        ("127.0.0.1", port), partial(handler, directory=str(out_dir)))


def serve(db_path, out_dir: Path | str = Path("out"), port: int = 8765,
          local_path: Path | str = LOCAL_RULES_PATH,
          open_browser: bool = True) -> None:
    conn = db_mod.connect(db_path)  # 起站先重生，保證頁面是最新資料
    try:
        export.write_export(conn, out_dir=out_dir,
                            classifier=Classifier(local_path=Path(local_path)))
    finally:
        conn.close()
    httpd = make_server(db_path, out_dir, port, local_path)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/dashboard.html"
    print(f"serve 模式：{url}")
    print("頁面上的「存檔」會寫回 categories.local.json 並重生資料；"
          "只綁本機、不對外；Ctrl+C 結束。")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
