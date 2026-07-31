"""`captures/<目錄>/index.json` —— 擷取索引的單一定義。

兩個寫入端（`netcapture.Capture` 錄真實瀏覽、`sites/einvoice_fetch._Sink`
重放 API）與兩個讀取端（`einvoice.ingest` 靠 `file` 對回檔案、`handoff` 印
去值化摘要）以前各自組同一份 dict，於是漂了：`_Sink` 硬寫 `status=200`、把
合成標籤放進 `url`，`handoff` 對 fetch 產物就把檔名字根印成端點。而且那不是
「它不知道」——真實的 url 與 status 就在呼叫點的 scope 裡，只是沒被傳進去。

**JSON 欄位名不可更動**：`captures/` 是重新解析的來源（明細只保存近半年，
重抓不回來），既有目錄必須照樣讀得動。這個模組只收斂 Python 端的建構與讀取，
落到磁碟的形狀與以前逐位元組相同。

這個模組不 import 其他 twcrawl 模組，也不碰 Playwright——解析端不裝瀏覽器
也要能讀索引。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

FILENAME = "index.json"


class Entry(NamedTuple):
    """一個擷取項目。

    只有 `file` 與 `post_data` 是承重的：前者讓 ingest 把回應對回檔案，
    後者藏著明細回應缺少的發票號碼（JWT payload）。其餘欄位供 handoff 顯示。
    """
    seq: int
    kind: str                      # "response" | "download"
    file: str                      # 相對擷取目錄，一律正斜線
    url: str = ""
    method: str = ""
    status: int | None = None
    content_type: str = ""
    bytes: int = 0
    post_data: str = ""            # JSON 裡叫 request_post_data

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Entry:
        # 舊目錄可能少欄位（下載項就沒有 method／status／content_type），
        # 一律給預設值而不是 KeyError
        return cls(
            seq=int(d.get("seq") or 0),
            kind=str(d.get("kind") or "response"),
            # Windows 寫出來的是反斜線，讀取端一律正規化成正斜線
            file=str(d.get("file") or "").replace("\\", "/"),
            url=str(d.get("url") or ""),
            method=str(d.get("method") or ""),
            status=d.get("status"),
            content_type=str(d.get("content_type") or ""),
            bytes=int(d.get("bytes") or 0),
            post_data=str(d.get("request_post_data") or ""),
        )

    def to_json(self) -> dict[str, Any]:
        if self.kind == "download":     # 下載項本來就只有這幾欄，保持原樣
            d: dict[str, Any] = {"seq": self.seq, "kind": self.kind,
                                 "url": self.url, "file": self.file,
                                 "bytes": self.bytes}
        else:
            d = {"seq": self.seq, "kind": self.kind, "url": self.url,
                 "method": self.method, "status": self.status,
                 "content_type": self.content_type, "bytes": self.bytes,
                 "file": self.file}
        if self.post_data:
            d["request_post_data"] = self.post_data
        return d


class Index:
    """一份擷取索引。每加一項就寫檔——中途 Ctrl+C 或當機都不會丟索引。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.entries: list[Entry] = []

    @property
    def next_seq(self) -> int:
        """下一個序號。檔名要帶序號，所以呼叫端在寫檔前先問。"""
        return len(self.entries) + 1

    def response(self, *, url: str, method: str, status: int | None,
                 content_type: str, size: int, file: Path | str,
                 post_data: str = "") -> Entry:
        return self._add(Entry(
            seq=self.next_seq, kind="response", file=self._rel(file),
            url=url, method=method, status=status,
            content_type=content_type, bytes=size, post_data=post_data))

    def download(self, *, url: str, file: Path | str, size: int) -> Entry:
        return self._add(Entry(seq=self.next_seq, kind="download",
                               file=self._rel(file), url=url, bytes=size))

    def _rel(self, file: Path | str) -> str:
        p = Path(file)
        rel = p.relative_to(self.root) if p.is_absolute() else p
        return str(rel).replace("\\", "/")

    def _add(self, e: Entry) -> Entry:
        self.entries.append(e)
        self.flush()
        return e

    def flush(self) -> None:
        (self.root / FILENAME).write_text(
            json.dumps([e.to_json() for e in self.entries],
                       ensure_ascii=False, indent=2),
            encoding="utf-8")


def path(capture_dir: Path | str) -> Path:
    return Path(capture_dir) / FILENAME


def read_entries(capture_dir: Path | str) -> list[Entry]:
    """依序的完整清單。缺檔或內容壞掉都回空——呼叫端要區分的話自己先查
    `path().exists()`（handoff 就是這麼做，它要給不同的訊息）。"""
    p = path(capture_dir)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [Entry.from_json(d) for d in raw if isinstance(d, dict)]


def by_file(capture_dir: Path | str) -> dict[str, Entry]:
    """{回應檔相對路徑: Entry}——ingest 拿它把掃到的檔案對回索引項。"""
    return {e.file: e for e in read_entries(capture_dir) if e.file}
