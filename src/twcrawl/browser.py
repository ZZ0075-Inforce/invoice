"""Playwright session 管理：人工登入、session 保存與重用。"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

def _chromium_executable() -> str | None:
    """允許指定既有的 Chromium，避免 playwright 版本與瀏覽器版本不符。

    一般情況下留空即可（跑過 `playwright install chromium` 就會自動找到）。
    """
    override = os.environ.get("TWCRAWL_CHROMIUM")
    if override:
        return override
    prebuilt = Path("/opt/pw-browsers/chromium")
    return str(prebuilt) if prebuilt.exists() else None


@contextlib.contextmanager
def browser_context(
    session_file: Path | str | None = None,
    headed: bool = True,
    slow_mo: int = 0,
):
    """開一個 browser context，若 session 檔存在就載入既有登入狀態。

    session_file 為 None 時代表不需要登入狀態（例如公開頁面）。路徑由呼叫端
    從工作區推出（`ws.state_path(名稱)`）——這個模組不知道工作區在哪。
    """
    storage = None
    if session_file:
        p = Path(session_file)
        if p.exists():
            storage = str(p)

    with sync_playwright() as pw:
        launch_kwargs: dict = {"headless": not headed, "slow_mo": slow_mo}
        if exe := _chromium_executable():
            launch_kwargs["executable_path"] = exe
        browser: Browser = pw.chromium.launch(**launch_kwargs)

        # headless 的預設 UA 帶 "HeadlessChrome"，gov.tw 的 WAF 會直接回
        # "The URL you requested has been blocked"。換成一般 Chrome UA 即可通過。
        user_agent = None
        if not headed:
            probe_ctx = browser.new_context()
            ua = probe_ctx.new_page().evaluate("navigator.userAgent")
            probe_ctx.close()
            if "HeadlessChrome" in ua:
                user_agent = ua.replace("HeadlessChrome", "Chrome")

        ctx: BrowserContext = browser.new_context(
            storage_state=storage,
            user_agent=user_agent,
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
        )
        ctx.set_default_timeout(30_000)
        try:
            yield ctx
        finally:
            with contextlib.suppress(Exception):
                ctx.close()
            with contextlib.suppress(Exception):
                browser.close()


def save_session(ctx: BrowserContext, session_file: Path | str) -> Path:
    p = Path(session_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    ctx.storage_state(path=str(p))
    p.chmod(0o600)  # 內含登入 cookie，限制權限
    return p


def wait_for_operator(message: str, pump: Page | None = None) -> None:
    """在終端機等待人工確認。用於人工登入 / 人工操作的交接點。

    Playwright 同步 API 的事件處理器（網路攔截、下載）只有在主執行緒
    進入 Playwright 呼叫時才會被派發——卡在 input()/time.sleep() 期間
    一律不執行。因此凡是等待期間需要錄東西的流程（capture），必須把
    page 傳進 pump，讓等待迴圈以 wait_for_timeout 持續讓事件流動。

    設定環境變數 TWCRAWL_DONE_FILE 時改為輪詢該檔案是否出現（給無互動
    終端機的環境用，例如由代理程式在背景啟動、使用者只操作瀏覽器）。
    """
    print()
    print("=" * 68)
    print(message)
    print("=" * 68, flush=True)

    signal = os.environ.get("TWCRAWL_DONE_FILE")
    done = threading.Event()
    aborted = threading.Event()

    if signal:
        print(f">>> 等待訊號檔案出現：{signal}", flush=True)
    else:
        def _stdin_waiter() -> None:
            try:
                input(">>> 完成後請回到這個終端機視窗，按 Enter 繼續（Ctrl+C 中止）… ")
            except (EOFError, KeyboardInterrupt):
                aborted.set()
            done.set()

        threading.Thread(target=_stdin_waiter, daemon=True).start()

    while True:
        if signal:
            p = Path(signal)
            if p.exists():
                with contextlib.suppress(OSError):
                    p.unlink()
                return
        elif done.is_set():
            if aborted.is_set():
                # 拋 KeyboardInterrupt 而不是 SystemExit：update 的步驟 runner
                # 會把 SystemExit 當成「這一步失敗」記錄後續跑下一步，但人工
                # 按 Ctrl+C 中止時應該停掉整輪。兩者必須在型別上可分辨。
                raise KeyboardInterrupt
            return
        if pump is not None:
            try:
                pump.wait_for_timeout(1000)  # 讓事件持續派發
            except Exception:
                time.sleep(1)  # 頁面被使用者關掉時退回純等待
        else:
            time.sleep(1)
