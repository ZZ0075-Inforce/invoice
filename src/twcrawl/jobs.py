"""控制台的背景工作：以子行程執行 twcrawl 指令，邊跑邊收輸出。

**為什麼是子行程，而不是在 serve 進程內直接呼叫指令層**（issue #20）：

1. serve 是長駐進程。改過 Python 之後不重啟，就會用到進程內的舊模組——
   2026-07-29 真的發生過（分類規則的變更被舊 serve 的重生蓋回去）。子行程
   每次都是重新 import 的最新程式碼，**經由這裡啟動的工作**不受那個坑影響。
   （注意範圍：`/api/rules` 的存檔重生仍走進程內的 `export.write_export`，
   那條路徑照舊要重啟 serve。這裡沒有把整個 serve 的問題解掉。）
2. Playwright 的同步 API 對事件迴圈有自己的假設（見 CLAUDE.md「關鍵教訓」），
   不該和 HTTP server 的執行緒模型混在一起。長工（update／fetch）真的會開
   瀏覽器，issue #21 之後這條路上跑的就是它們。
3. 進度直接串子行程的 stdout，指令層既有的 print 一行都不用改。

這個模組不知道有 HTTP 這回事，只認得「工作名稱＋參數 → 一組 argv」與
「工作區目錄」。它也不 import 其他 twcrawl 模組，唯一的例外是
`operator_signal`（純 stdlib、沒有領域知識）——人工交接的環境變數名與標記行
是這個行程與子行程之間的協定，兩端必須是同一份定義。
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal as signal_mod
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable

from . import operator_signal

MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")

# 一個工作＝依序跑的幾道指令（多半只有一道）。**這是刻意放在這一層的**：
# 「匯入完要重生報表」如果交給頁面在工作結束後再送一次請求，使用者關掉分頁
# 就靜默留下舊報表——而使用者不會知道自己看到的是舊的。
Steps = list[list[str]]


class BadParams(ValueError):
    """參數不合格。訊息是人話，會原樣送回頁面顯示。"""


class Busy(RuntimeError):
    """已經有工作在跑。同時只允許一個：兩個 export 互相蓋 out/ 沒有意義，
    兩個 fetch 搶同一個登入 session 更沒有。"""


def _no_params(argv: list[str], title: str) -> Callable[[dict], tuple[Steps, str]]:
    def build(params: dict) -> tuple[Steps, str]:
        if params:
            raise BadParams(f"「{title}」不吃參數")
        return [list(argv)], title
    return build


def _month(params: dict, key: str, label: str) -> str:
    value = str(params.get(key, "")).strip()
    if not MONTH_RE.match(value):
        raise BadParams(f"{label}要填 YYYY-MM（例如 2026-01），收到「{value}」")
    return value


def _fetch(params: dict) -> tuple[Steps, str]:
    a = _month(params, "from", "起始月份")
    b = _month(params, "to", "結束月份")
    if a > b:
        raise BadParams(f"起始月份（{a}）不能晚於結束月份（{b}）")
    return [["fetch", "--from", a, "--to", b]], f"抓發票 {a} ～ {b}"


def _import(params: dict) -> tuple[Steps, str]:
    """匯入指定的 CSV，**接著重生報表**。

    路徑沒有辦法像月份那樣驗形狀（任何字串都可能是合法路徑），所以這裡只擋
    「會被誤讀成別的東西」的那幾種，檔案本身的合法性交給指令端講人話——那份
    訊息已經寫好了，抄第二份只會讓兩邊漂掉（issue #22 的驗收條件也這麼要求）。
    """
    raw = str(params.get("path", "")).strip()
    # 檔案總管的「複製檔案路徑」會連雙引號一起給，直接貼進來是常態
    raw = raw.strip('"').strip("'").strip()
    if not raw:
        raise BadParams("要先指定 CSV 檔案的路徑")
    if raw.startswith("-"):
        # argparse 會把它當成旗標吃掉：`import --help` 印完說明以 0 收場，
        # 看起來就像匯入成功了
        raise BadParams("路徑不能以 - 開頭")
    if "\n" in raw or "\r" in raw:
        raise BadParams("路徑不能含換行——請只貼一個檔案的路徑")
    return [["import", raw], ["export", "--no-open"]], f"匯入 {Path(raw).name}"


# 控制台能啟動的工作白名單。**端點收到的是名稱與具名參數、不是 argv**——
# 頁面永遠沒有機會拼出一段命令列，這是這層唯一的注入防線。參數也不是原樣
# 串進去：每個值都由上面的 builder 驗過形狀（月份必須是 YYYY-MM），argv
# 的組法留在這個模組裡。
#
# export 帶 --no-open：在伺服器端替使用者開瀏覽器是錯的（工作可能是別的
# 分頁按下去的），頁面自己有連結。update 的 export 步驟同理。
#
# login 單獨也給一顆：token 過期時 fetch 會回 401 並要你「重跑 login」
# （CLAUDE.md「已知的坑」），控制台沒有這顆的話那句話就沒有出口。
ALLOWED: dict[str, Callable[[dict], tuple[Steps, str]]] = {
    "export": _no_params(["export", "--no-open"], "重生報表"),
    "update": _no_params(["update", "--no-open"], "每月例行"),
    "login": _no_params(["login"], "登入"),
    "fetch": _fetch,
    "import": _import,
}

# 輸出保留上限。工作再長也不該把 serve 的記憶體吃光；超過就丟最舊的，
# 並把丟掉的行數一併回報——靜默截斷會讓人以為工作真的只印了這些。
MAX_LINES = 2000


def _kill_tree(proc: subprocess.Popen) -> None:
    """連子孫一起收掉。

    `proc.terminate()` 只殺得到直接的子行程（`python -m twcrawl`），但長工
    真正吃資源的是它底下的 Chromium。只砍父的話，使用者看到的是「已中止」，
    工作管理員裡卻還留著一票 chrome.exe 抱著登入中的頁面——正是驗收條件
    寫的「半死的子行程」。
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # Windows 沒有行程群組可殺；taskkill /T 依父子關係整棵收掉
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal_mod.SIGKILL)
    except Exception:
        pass  # 下面的 kill() 是後備：至少直接的子行程一定要死
    with contextlib.suppress(Exception):
        proc.kill()


class Job:
    """一次執行。輸出由讀取執行緒寫入、由請求執行緒讀出，所以行緩衝要上鎖。"""

    def __init__(self, job_id: int, name: str, steps: Steps, title: str,
                 done_file: Path) -> None:
        self.id = job_id
        self.name = name
        self.steps = steps
        self.title = title
        self.done_file = done_file
        self.returncode: int | None = None
        self._lines: list[str] = []
        self._dropped = 0
        self._awaiting = False
        self._cancelled = False
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # 下面幾個 _locked 函式只在已經持有 self._lock 時呼叫——threading.Lock
    # 不可重入，讓它們自己再取一次鎖就是死鎖（#20 的 review 抓過一次）。
    def _state_locked(self) -> str:
        if self.returncode is None:
            return "running"
        if self._cancelled:
            return "cancelled"
        return "done" if self.returncode == 0 else "failed"

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def finish(self, returncode: int) -> None:
        """收尾。與 add／snapshot 共用同一個鎖：結束碼是「還在跑嗎」的唯一
        依據，讓它在鎖外寫的話，狀態的正確性就只靠寫入順序的巧合。"""
        with self._lock:
            self.returncode = returncode
            self._awaiting = False

    def add(self, line: str) -> None:
        with self._lock:
            if operator_signal.is_awaiting(line):
                self._awaiting = True
            self._lines.append(line)
            if len(self._lines) > MAX_LINES:
                drop = len(self._lines) - MAX_LINES
                del self._lines[:drop]
                self._dropped += drop

    def attach(self, proc: subprocess.Popen) -> bool:
        """記下子行程，並回報「中止在它起來之前就按下了嗎」。

        中止與啟動是兩條執行緒，這個回傳值就是那個競態的出口：先按中止再
        Popen 成功的話，沒有人會去殺它，工作會一路跑完卻顯示已中止。
        """
        with self._lock:
            self._proc = proc
            return self._cancelled

    @property
    def cancelled(self) -> bool:
        """中止按下了嗎。多段工作靠它在**兩段之間**收手——中止正好落在兩段
        中間時沒有行程可殺，不問這一句的話下一段照樣會起來。"""
        with self._lock:
            return self._cancelled

    def cancel(self) -> bool:
        """中止。回 False＝工作已經結束了，沒東西可中止（端點據此回 409）。"""
        with self._lock:
            if self.returncode is not None:
                return False
            self._cancelled = True
            proc = self._proc
        if proc is not None:
            _kill_tree(proc)
        return True

    def signal_operator(self) -> bool:
        """使用者按下「我已登入」：建立子行程正在等的訊號檔。

        回 False＝現在根本沒有在等人工（工作已結束、或別的分頁按過了）。
        端點據此回 409 而不是假裝成功——否則訊號檔會提早躺在那裡，等到
        login 真的問起時被立刻放行，人還沒動手就往下跑了。
        """
        with self._lock:
            if not self._awaiting or self.returncode is not None:
                return False
            operator_signal.send(self.done_file)  # 寫不出去就讓例外上去，
            self._awaiting = False                # 旗標不能先清掉
            return True

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
                "title": self.title,
                "state": self._state_locked(),
                "returncode": self.returncode,
                # 「在等人工」只有工作還活著時才成立
                "awaiting": self._awaiting and self.returncode is None,
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

    def start(self, name: str, cwd: Path, params: dict | None = None) -> Job:
        """啟動白名單內的工作。

        名稱不在白名單拋 KeyError、參數不合格拋 BadParams、已有工作拋 Busy。
        """
        if name not in ALLOWED:
            raise KeyError(name)
        # 先驗參數再搶位子：參數打錯不該把「有沒有工作在跑」的狀態動到
        steps, title = ALLOWED[name](dict(params or {}))
        with self._lock:
            if self._job is not None and self._job.state == "running":
                raise Busy(f"「{self._job.title}」還在跑，等它結束再開下一個。")
            self._seq += 1
            # 訊號檔放暫存目錄而不是工作區：它是這次執行的內部狀態，不該
            # 出現在使用者的資料夾裡，也不該有機會被收進備份包
            workdir = Path(tempfile.mkdtemp(prefix="twcrawl-job-"))
            job = Job(self._seq, name, steps, title, workdir / "done")
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
        # TWCRAWL_DONE_FILE：登入那一步改成等訊號檔，不等終端機的 Enter
        #   （協定見 operator_signal）。
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   **{operator_signal.ENV_DONE_FILE: str(job.done_file)})
        # 這個函式**一定**要替工作收尾。少了 finally，讀管線時的任何例外都會
        # 讓 returncode 永遠留在 None：狀態卡在 running、Runner 從此永久 Busy、
        # 頁面的按鈕永久 disabled，只能重啟 serve 才救得回來。
        rc = -1
        try:
            total = len(job.steps)
            for i, step in enumerate(job.steps, 1):
                if job.cancelled:      # 中止正好落在兩段之間：不要再起下一段
                    break
                if total > 1:
                    job.add(f"=== {i}/{total} twcrawl {' '.join(step)} ===")
                rc = self._run_step(job, cwd, env, step)
                if rc != 0:
                    break              # 前一段沒過，後面就別做了
        finally:
            job.finish(rc)
            shutil.rmtree(job.done_file.parent, ignore_errors=True)

    def _run_step(self, job: Job, cwd: Path, env: dict, args: list[str]) -> int:
        argv = [sys.executable, "-X", "utf8", "-m", "twcrawl", *args]
        rc = -1
        try:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), env=env,
                # stdin 一定要斷開：serve 可能是從終端機起的，子行程會繼承
                # 那個 tty，於是 update 的備份密碼 getpass 會停在沒人看的
                # 視窗上等輸入，控制台則永遠停在「執行中」。斷開之後
                # `backup_password` 判定為非互動、那一步以人話跳過。
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                # POSIX：自成一個行程群組，中止時才殺得掉整棵（Windows 走
                # taskkill /T，不需要這個旗標）
                **({} if os.name == "nt" else {"start_new_session": True}),
            )
        except OSError as exc:  # 直譯器不見、cwd 不存在……
            job.add(f"啟動失敗：{exc}")
            return rc
        try:
            if job.attach(proc):   # 中止比 Popen 早一步按下
                _kill_tree(proc)
            assert proc.stdout is not None
            for line in proc.stdout:
                job.add(line.rstrip("\n"))
            proc.stdout.close()
            return proc.wait()
        except Exception as exc:  # 管線讀壞、行程被外力砍掉……
            job.add(f"工作中斷：{exc}")
            _kill_tree(proc)      # 別留下沒人收的孤兒行程（含底下的瀏覽器）
            return proc.wait()
