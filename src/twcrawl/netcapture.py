"""網路層攔截。

SPA 的資料一定經由 XHR/fetch 回傳，攔截網路回應比解析 DOM 穩定得多：
不受前端改版、CSS class 變動影響，而且直接拿到結構化資料。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import BrowserContext, Download, Response

from .capture_index import Index

# 值得留下來的回應類型
INTERESTING_CT = re.compile(
    r"application/json|text/json|text/csv|application/vnd\.ms-excel|"
    r"application/vnd\.openxmlformats|application/octet-stream",
    re.I,
)
# 明顯是靜態資源的，直接略過
BORING_URL = re.compile(
    r"\.(js|mjs|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|map)(\?|$)", re.I
)


def _safe(s: str, limit: int = 80) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:limit].strip("_") or "x"


@dataclass
class Capture:
    """把一次瀏覽期間所有值得看的回應寫到指定的擷取目錄底下。

    目錄由 `Workspace.new_capture(名稱)` 產生（已含 responses/ 與 downloads/）
    ——命名與建目錄只有那一份定義，fetch 那條路徑用的是同一個。
    """

    root: Path
    index: Index = field(init=False)

    def __post_init__(self) -> None:
        self.index = Index(self.root)

    # -- 掛載 ----------------------------------------------------------------
    def attach(self, ctx: BrowserContext) -> None:
        ctx.on("response", self._on_response)
        ctx.on("page", lambda page: page.on("download", self._on_download))
        for page in ctx.pages:
            page.on("download", self._on_download)

    # -- handlers ------------------------------------------------------------
    def _on_response(self, resp: Response) -> None:
        try:
            url = resp.url
            if BORING_URL.search(url) or url.startswith("data:"):
                return
            ct = (resp.headers or {}).get("content-type", "")
            # SPA 的資料一定走 xhr/fetch，一律錄下、不看 content-type——
            # 政府網站常把 JSON 標成 text/plain 或 text/html，只認 CT 會整批漏接
            try:
                rtype = resp.request.resource_type
            except Exception:
                rtype = ""
            if rtype not in ("xhr", "fetch") and not INTERESTING_CT.search(ct):
                return
            body = resp.body()
        except Exception:
            return  # 回應可能已被回收（redirect / 已關閉的 page），略過即可

        if not body:
            return

        stem = f"{self.index.next_seq:03d}_{_safe(url.split('?')[0].rsplit('/', 1)[-1])}"
        sniff = body.lstrip()[:1]
        ext = ".json" if ("json" in ct.lower() or sniff in (b"{", b"[")) else ".bin"
        path = self.root / "responses" / f"{stem}{ext}"
        path.write_bytes(body)

        # 順手記下 POST body，之後要改成全自動時就知道要送什麼參數
        post = ""
        try:
            if resp.request.method == "POST":
                post = (resp.request.post_data or "")[:2000]
        except Exception:
            pass

        self.index.response(url=url, method=resp.request.method,
                            status=resp.status, content_type=ct,
                            size=len(body), file=path, post_data=post)
        print(
            f"  [捕獲] {resp.request.method} {resp.status} {len(body):>7,}B  {url[:96]}",
            flush=True,
        )

    def _on_download(self, dl: Download) -> None:
        try:
            target = self.root / "downloads" / _safe(dl.suggested_filename, 120)
            dl.save_as(str(target))
        except Exception as e:  # 下載可能被使用者取消
            print(f"  [下載失敗] {e}")
            return
        self.index.download(url=dl.url, file=target,
                            size=target.stat().st_size)
        print(f"  [下載] {target.name} ({target.stat().st_size:,}B)", flush=True)

    # -- 收尾 ----------------------------------------------------------------
    def finish(self) -> Path:
        self.index.flush()
        return self.root
