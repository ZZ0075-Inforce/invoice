"""指令層：每個 CLI 指令一個可呼叫的函式，以及 update 的步驟清單。

指令函式吃明確的具名參數（不吃 argparse 的 Namespace）、回傳結果 dict、
不印摘要。這樣 `twcrawl <cmd>` 與 update 的第 N 步呼叫的是**同一個函式、
同一份預設值**——接線只有一份，不會兩邊各打一遍而漂移（舊版 update 曾
因此少了登入前置檢查、少了 --no-cloud、把 max_pages=500 重打一次）。

摘要由 cli.py 的格式化層印。長時間操作的即時進度（下載、逐頁、逐月）
仍由底層 module 直接印——那是必要的回饋，不走回傳值。

路徑沿用既有慣例（out_dir 之類的具名預設參數），沒有集中的路徑物件；
那是另一件事。

純委派的指令（serve / bizreg / geocode / probe）刻意留在 cli.py 直接
呼叫：包一層不會讓任何複雜度集中，只是多一層。
"""

from __future__ import annotations

import datetime as _dt
import getpass
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import backup as backup_mod
from . import export, handoff, lottery
from .browser import browser_context, state_path
from .match import run_match
from .netcapture import Capture
from .sites import einvoice, einvoice_fetch, fda

# update 的 FDA 回溯天數。清單只需涵蓋近期公告；比對本身不受此限——
# 一張舊發票照樣會命中今天才公告的下架品，所以 match 不吃這個 since。
FDA_LOOKBACK_DAYS = 90

# 資料庫是空的時候 update 往回抓幾個月（明細保存期約近 6 個月）
EMPTY_DB_LOOKBACK_MONTHS = 5


# ---- 共用小工具 ----------------------------------------------------------

def latest_capture(captures_dir: Path | str = Path("captures")) -> Path:
    """最近一次擷取目錄。

    按 mtime 取，不按檔名字典序：capture 產生 `einvoice-<時間戳>`、fetch
    產生 `einvoice-fetch-<時間戳>`，字典序下 "einvoice-f" 恆大於
    "einvoice-2"，永遠選不到比較新的人工 capture。
    """
    roots = [p for p in Path(captures_dir).glob("einvoice-*") if p.is_dir()]
    if not roots:
        raise SystemExit("找不到任何擷取結果，請先執行 `twcrawl capture`。")
    return max(roots, key=lambda p: p.stat().st_mtime)


def auto_month_range(last_inv_date: str | None, today: _dt.date) -> tuple[str, str]:
    """update 的抓取區間：從資料庫最新發票的月份（重抓補漏，upsert 冪等）
    到當月；資料庫是空的就回推 5 個月（明細保存期內）。"""
    to = f"{today:%Y-%m}"
    if last_inv_date:
        return str(last_inv_date)[:7], to
    y, m = today.year, today.month - EMPTY_DB_LOOKBACK_MONTHS
    if m <= 0:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}", to


def backup_password() -> str | None:
    """備份密碼：環境變數優先，其次互動輸入；非互動終端機回 None。"""
    pw = os.environ.get("TWCRAWL_BACKUP_PASSWORD")
    if pw:
        return pw
    if not sys.stdin or not sys.stdin.isatty():
        return None
    return getpass.getpass("備份密碼（留空跳過備份）：")


def open_dashboard(path: Path | str) -> None:
    """export 完直接開瀏覽器看（file:// 檢視是日常路徑，見 docs/adr/0002）。"""
    import webbrowser
    try:
        webbrowser.open(Path(path).resolve().as_uri())
    except Exception as e:  # 開不了瀏覽器不該讓整個指令失敗
        print(f"！無法自動開啟儀表板（{e}），請自行開 {path}")


def _require_session() -> None:
    if not state_path(einvoice.SESSION).exists():
        raise SystemExit("找不到登入狀態，請先執行 `twcrawl login`。")


# ---- 指令 ----------------------------------------------------------------

def cmd_login(*, headed: bool = True, slow_mo: int = 0) -> dict:
    with browser_context(session=einvoice.SESSION, headed=headed,
                         slow_mo=slow_mo) as ctx:
        einvoice.login(ctx)
    return {"session": str(state_path(einvoice.SESSION))}


def cmd_capture(conn, *, headed: bool = True) -> dict:
    cap = Capture("einvoice")
    with browser_context(session=einvoice.SESSION, headed=headed) as ctx:
        root = einvoice.capture(ctx, cap)
    return {"dir": str(root), **einvoice.ingest(root, conn)}


def cmd_fetch(conn, date_from: str, date_to: str, *,
              with_details: bool = True, headed: bool = False) -> dict:
    # 前置檢查放在這裡，subcommand 與 update 才會一致地快速失敗——
    # 少了它，`update --no-login` 會一路跑到 einvoice_fetch 才死。
    _require_session()
    with browser_context(session=einvoice.SESSION, headed=headed) as ctx:
        return einvoice_fetch.fetch_range(ctx, conn, date_from, date_to,
                                          with_details=with_details)


def cmd_ingest(conn, *, capture_dir: Path | str | None = None) -> dict:
    root = Path(capture_dir) if capture_dir else latest_capture()
    print(f"解析 {root}")
    return {"dir": str(root), **einvoice.ingest(root, conn)}


def cmd_handoff(*, capture_dir: Path | str | None = None,
                out_dir: Path | str = Path("out")) -> dict:
    root = Path(capture_dir) if capture_dir else latest_capture()
    text = handoff.summarize(root)
    out_path = Path(out_dir) / f"handoff_{root.name}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return {"dir": str(root), "text": text, "path": str(out_path)}


def cmd_fda(conn, *, source: str = "all", url: str | None = None,
            since: str | None = None, max_pages: int = 500,
            headed: bool = False, out_dir: Path | str = Path("out")) -> dict:
    if url:
        selected = {"custom": url}
    elif source == "all":
        selected = dict(fda.SOURCES)
    else:
        selected = {source: fda.SOURCES[source]}

    results: dict[str, dict] = {}
    with browser_context(session=None, headed=headed) as ctx:
        page = ctx.new_page()
        for name, src_url in selected.items():
            print(f"\n=== 來源：{name} ===")
            # since 只給 feed 型來源：事件型清單不依日期排序，提前停止
            # 翻頁會任意截斷整份強制下架清單（見 fda.SOURCE_META）
            src_since = since if (since and fda.is_feed(name)) else None
            if since and not src_since:
                print(f"  （{name} 非 feed 型來源，不適用回溯日期，整份擷取）")
            results[name] = fda.fetch(page, conn, url=src_url,
                                      max_pages=max_pages, since=src_since,
                                      name=name, out_dir=Path(out_dir))
    return {"sources": results}


def cmd_match(conn, *, since: str | None = None,
              out_dir: Path | str = Path("out")) -> dict:
    return run_match(conn, since=since, out_dir=Path(out_dir))


def cmd_lottery(conn, *, fetch: bool = True, cloud: bool = True) -> dict:
    return lottery.run_lottery(conn, fetch=fetch, cloud=cloud)


def cmd_export(conn, *, out_dir: Path | str = Path("out"),
               open_browser: bool = True) -> dict:
    dash = export.write_export(conn, out_dir=Path(out_dir))
    if open_browser:
        open_dashboard(dash)
    return {"dashboard": str(dash)}


def cmd_backup(password: str, *, db_path: Path | str,
               out_dir: Path | str = backup_mod.BACKUP_DIR) -> dict:
    path = backup_mod.make_backup(password, db_path=db_path, out_dir=out_dir)
    return {"path": str(path)}


# ---- update：步驟清單 ----------------------------------------------------

@dataclass
class Step:
    """update 的一步。skip_reason 非空就不執行，但仍佔一個編號。"""

    label: str
    run: Callable[[], dict] | None = None
    skip_reason: str = ""


@dataclass
class StepOutcome:
    label: str
    detail: str = ""


def run_steps(steps: list[Step]) -> dict:
    """依序執行；一步失敗就記錄後續跑，最後彙總。

    七步的產物都即時落地到資料庫，所以就算 fetch 掛了，後面幾步拿庫內
    既有資料重生仍然有意義（儀表板本來就有「已 N 天沒有新發票」的
    staleness 橫幅會告訴你資料是舊的）。一個 FDA 來源暫時掛掉不該連帶
    砍掉對獎與儀表板。

    攔 SystemExit 是必要的：它是這個 codebase 的主要錯誤通道（fda、
    einvoice_fetch、backup、bizreg…），`except Exception` 攔不到。
    KeyboardInterrupt 則刻意不攔——人工中止要停掉整輪。
    """
    total = len(steps)
    results: dict[str, dict] = {}
    failed: list[StepOutcome] = []
    skipped: list[StepOutcome] = []
    for i, step in enumerate(steps, 1):
        if step.skip_reason or step.run is None:
            reason = step.skip_reason or "沒有可執行的內容"
            print(f"\n=== {i}/{total} {step.label}（跳過：{reason}） ===")
            skipped.append(StepOutcome(step.label, reason))
            continue
        print(f"\n=== {i}/{total} {step.label} ===")
        try:
            results[step.label] = step.run() or {}
        except KeyboardInterrupt:
            raise
        except (Exception, SystemExit) as e:
            msg = str(e).strip() or type(e).__name__
            print(f"！{step.label} 失敗：{msg}")
            failed.append(StepOutcome(step.label, msg))
    return {"total": total, "results": results, "failed": failed,
            "skipped": skipped}


def update_steps(conn, *, db_path: Path | str, login: bool = True,
                 backup: bool = True, cloud: bool = True,
                 open_browser: bool = True,
                 out_dir: Path | str = Path("out"),
                 password: str | None = None,
                 today: _dt.date | None = None) -> list[Step]:
    """組出 update 的七步。

    抓取區間與 FDA 回溯日期在組裝時就算好（兩者都不受 login 影響），
    所以這個函式是純的：給定選項就決定了哪幾步會跑、標籤長什麼樣。
    """
    today = today or _dt.date.today()
    last = conn.execute("select max(inv_date) from invoices").fetchone()[0]
    d_from, d_to = auto_month_range(last, today)
    since = (today - _dt.timedelta(days=FDA_LOOKBACK_DAYS)).isoformat()

    if not backup:
        backup_step = Step("backup", skip_reason="--no-backup")
    elif not password:
        backup_step = Step(
            "backup",
            skip_reason="沒有密碼來源（非互動且未設 TWCRAWL_BACKUP_PASSWORD）")
    else:
        backup_step = Step(
            "backup", lambda: cmd_backup(password, db_path=db_path))

    return [
        Step("login（請在瀏覽器完成登入）",
             lambda: cmd_login(),
             skip_reason="" if login else "--no-login"),
        Step(f"fetch {d_from} ～ {d_to}",
             lambda: cmd_fetch(conn, d_from, d_to)),
        Step(f"fda（feed 型來源回溯至 {since}）",
             lambda: cmd_fda(conn, since=since, out_dir=out_dir)),
        Step("match", lambda: cmd_match(conn, out_dir=out_dir)),
        Step("lottery", lambda: cmd_lottery(conn, cloud=cloud)),
        Step("export", lambda: cmd_export(conn, out_dir=out_dir,
                                          open_browser=open_browser)),
        backup_step,
    ]


def format_update(summary: dict) -> str:
    """update 的收尾彙總。"""
    done = summary["total"] - len(summary["failed"]) - len(summary["skipped"])
    out = [f"\n=== 完成 {done}/{summary['total']} 步 ==="]
    for s in summary["skipped"]:
        out.append(f"  － {s.label}：跳過（{s.detail}）")
    for f in summary["failed"]:
        out.append(f"  ✗ {f.label}：{f.detail}")
    if summary["failed"]:
        out.append("（失敗的步驟可以單獨重跑，其餘結果已經落地）")
    return "\n".join(out)
