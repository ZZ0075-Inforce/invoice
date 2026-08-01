"""控制台的背景工作：以子行程執行 twcrawl 指令，邊跑邊收輸出。

**為什麼是子行程，而不是在 serve 進程內直接呼叫指令層**（issue #20）：

1. serve 是長駐進程。改過 Python 之後不重啟，就會用到進程內的舊模組——
   2026-07-29 真的發生過（分類規則的變更被舊 serve 的重生蓋回去）。子行程
   每次都是重新 import 的最新程式碼，這個坑從結構上消失。
2. Playwright 的同步 API 對事件迴圈有自己的假設（見 CLAUDE.md「關鍵教訓」），
   不該和 HTTP server 的執行緒模型混在一起。fetch 之類的長工遲早要走這條路。
3. 進度直接串子行程的 stdout，指令層既有的 print 一行都不用改。

這個模組不 import 其他 twcrawl 模組，也不知道有 HTTP 這回事——它只認得
「工作名稱 → 一組參數」與「工作區目錄」。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

# 控制台能啟動的指令白名單。**端點收到的是名字、不是 argv**——頁面永遠沒有
# 機會拼出一段命令列，這是這層唯一的注入防線。
# export 帶 --no-open：在伺服器端替使用者開瀏覽器是錯的（工作可能是別的
# 分頁按下去的），頁面自己有連結。
ALLOWED: dict[str, list[str]] = {
    "export": ["export", "--no-open"],
}

# 輸出保留上限。工作再長也不該把 serve 的記憶體吃光；超過就丟最舊的，
# 並把丟掉的行數一併回報——靜默截斷會讓人以為工作真的只印了這些。
MAX_LINES = 2000


class Busy(RuntimeError):
    """已經有工作在跑。同時只允許一個：兩個 export 互相蓋 out/ 沒有意義。"""


class Job:
    """一次執行。輸出由讀取執行緒寫入、由請求執行緒讀出，所以行緩衝要上鎖。"""

    def __init__(self, job_id: int, name: str, args: list[str]) -> None:
        self.id = job_id
        self.name = name
        self.args = args
        self.returncode: int | None = None
        self._lines: list[str] = []
        self._dropped = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        if self.returncode is None:
            return "running"
        return "done" if self.returncode == 0 else "failed"

    def add(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > MAX_LINES:
                drop = len(self._lines) - MAX_LINES
                del self._lines[:drop]
                self._dropped += drop

    def snapshot(self, since: int = 0) -> dict:
        """since＝呼叫端已經拿過幾行的游標。

        長工輪詢不該每次重傳全部輸出，所以回傳的是增量與新游標 `next`。
        游標算的是「這個工作至今總共產生幾行」，因此即使前面的行已被丟棄，
        游標也不會倒退。
        """
        with self._lock:
            start = max(0, since - self._dropped)
            return {
                "id": self.id,
                "name": self.name,
                "state": self.state,
                "returncode": self.returncode,
                "lines": self._lines[start:],
                "next": self._dropped + len(self._lines),
                "dropped": self._dropped,
            }


class Runner:
    """同時只跑一個工作。

    serve 是 ThreadingHTTPServer，每個請求各一條執行緒，所以「現在有沒有在跑」
    的判斷與換手必須在同一個鎖裡完成——分兩步查再設，兩個請求會同時通過。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._seq = 0

    def current(self) -> Job | None:
        with self._lock:
            return self._job

    def start(self, name: str, cwd: Path) -> Job:
        """啟動白名單內的工作。名稱不在白名單拋 KeyError，已有工作拋 Busy。"""
        if name not in ALLOWED:
            raise KeyError(name)
        with self._lock:
            if self._job is not None and self._job.state == "running":
                raise Busy(f"「{self._job.name}」還在跑，等它結束再開下一個。")
            self._seq += 1
            job = Job(self._seq, name, list(ALLOWED[name]))
            self._job = job
        threading.Thread(target=self._run, args=(job, Path(cwd)),
                         daemon=True).start()
        return job

    def _run(self, job: Job, cwd: Path) -> None:
        # PYTHONIOENCODING／-X utf8：輸出走管線時 Python 會改用地區編碼
        #   （zh-TW Windows 是 cp950），指令裡的中文與 emoji 會炸成
        #   UnicodeEncodeError——而且是在子行程裡炸，看起來像指令本身壞掉。
        # PYTHONUNBUFFERED：不設的話輸出是塊狀緩衝，進度要等工作結束才一次
        #   冒出來，「即時顯示」就成了空話。
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        argv = [sys.executable, "-X", "utf8", "-m", "twcrawl", *job.args]
        try:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:  # 直譯器不見、cwd 不存在……
            job.add(f"啟動失敗：{exc}")
            job.returncode = -1
            return
        assert proc.stdout is not None
        for line in proc.stdout:
            job.add(line.rstrip("\n"))
        proc.stdout.close()
        job.returncode = proc.wait()
