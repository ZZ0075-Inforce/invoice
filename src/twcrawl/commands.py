"""指令層：每個 CLI 指令一個可呼叫的函式，以及 update 的步驟清單。

指令函式吃明確的具名參數（不吃 argparse 的 Namespace）、回傳結果 dict、
不印摘要。這樣 `twcrawl <cmd>` 與 update 的第 N 步呼叫的是**同一個函式、
同一份預設值**——接線只有一份，不會兩邊各打一遍而漂移（舊版 update 曾
因此少了登入前置檢查、少了 --no-cloud、把 max_pages=500 重打一次）。

摘要由 cli.py 的格式化層印。長時間操作的即時進度（下載、逐頁、逐月）
仍由底層 module 直接印——那是必要的回饋，不走回傳值。

路徑一律來自 `Workspace`（CONTEXT.md「工作區」）：指令函式吃 ws，不吃
CWD 錨定的預設值。`ws.out` 之類的推導只有工作區知道，指令層不重打一遍。

純委派的指令（serve / bizreg / geocode / probe）刻意留在 cli.py 直接
呼叫：包一層不會讓任何複雜度集中，只是多一層。
"""

from __future__ import annotations

import datetime as _dt
import getpass
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import backup as backup_mod
from . import db, export, handoff, lottery
from .browser import browser_context
from .categories import Classifier
from .match import run_match
from .netcapture import Capture
from .sites import einvoice, einvoice_fetch, fda
from .workspace import Workspace

# update 的 FDA 回溯天數。清單只需涵蓋近期公告；比對本身不受此限——
# 一張舊發票照樣會命中今天才公告的下架品，所以 match 不吃這個 since。
FDA_LOOKBACK_DAYS = 90

# 資料庫是空的時候 update 往回抓幾個月（明細保存期約近 6 個月）
EMPTY_DB_LOOKBACK_MONTHS = 5


# ---- 共用小工具 ----------------------------------------------------------

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


def backup_password(prompt: str = "備份密碼（留空跳過備份）：") -> str | None:
    """備份密碼：環境變數優先，其次互動輸入；非互動終端機回 None。

    backup 與 restore 是同一個密碼，所以環境變數名與「非互動就回 None」的
    規則只有這一份；只有提示語不同（restore 留空不是「跳過」而是做不下去）。
    """
    pw = os.environ.get("TWCRAWL_BACKUP_PASSWORD")
    if pw:
        return pw
    if not sys.stdin or not sys.stdin.isatty():
        return None
    return getpass.getpass(prompt)


def open_dashboard(path: Path | str) -> None:
    """export 完直接開瀏覽器看（file:// 檢視是日常路徑，見 docs/adr/0002）。"""
    import webbrowser
    try:
        webbrowser.open(Path(path).resolve().as_uri())
    except Exception as e:  # 開不了瀏覽器不該讓整個指令失敗
        print(f"！無法自動開啟儀表板（{e}），請自行開 {path}")


def _require_session(ws: Workspace) -> None:
    if not ws.state_path(einvoice.SESSION).exists():
        raise SystemExit("找不到登入狀態，請先執行 `twcrawl login`。")


def _classifier(ws: Workspace) -> Classifier:
    """建分類器；沒有個人規則檔就講一句。

    以前規則檔不存在是完全靜默的——在錯的目錄跑 export，個人規則全部消失
    而畫面上看不出來。函式庫維持不印字，提示由指令層發出。
    """
    if not ws.rules.exists():
        print(f"！沒有個人規則（{ws.rules} 不存在）"
              "——店家將只靠通用規則與稅籍行業分類")
    return Classifier(ws.rules)


# ---- 指令 ----------------------------------------------------------------

def cmd_login(ws: Workspace, *, headed: bool = True, slow_mo: int = 0) -> dict:
    session_file = ws.state_path(einvoice.SESSION)
    with browser_context(session_file=session_file, headed=headed,
                         slow_mo=slow_mo) as ctx:
        einvoice.login(ctx, session_file)
    return {"session": str(session_file)}


def cmd_capture(conn, ws: Workspace, *, headed: bool = True) -> dict:
    cap = Capture(ws.new_capture("einvoice"))
    with browser_context(session_file=ws.state_path(einvoice.SESSION),
                         headed=headed) as ctx:
        root = einvoice.capture(ctx, cap)
    return {"dir": str(root), **einvoice.ingest(root, conn)}


def cmd_fetch(conn, ws: Workspace, date_from: str, date_to: str, *,
              with_details: bool = True, headed: bool = False) -> dict:
    # 前置檢查放在這裡，subcommand 與 update 才會一致地快速失敗——
    # 少了它，`update --no-login` 會一路跑到 einvoice_fetch 才死。
    _require_session(ws)
    with browser_context(session_file=ws.state_path(einvoice.SESSION),
                         headed=headed) as ctx:
        return einvoice_fetch.fetch_range(
            ctx, conn, ws.new_capture("einvoice-fetch"), date_from, date_to,
            with_details=with_details)


def cmd_import(conn, ws: Workspace, path: Path | str) -> dict:
    """匯入一個平台匯出的 CSV。歷年資料唯一的入口（issue #19）。

    路徑是使用者指名的檔案，不是工作區推出來的——匯入的來源本來就在工作區
    外（下載目錄、隨身碟、專案申請寄回來的檔案），所以這裡收絕對／相對路徑
    都行，由 shell 的目前目錄決定。
    """
    return einvoice.import_csv(Path(path), conn)


def format_import(res: dict) -> str:
    kind = {"header": "含表頭的明細寬表", "md": "舊版 M/D 列型"}.get(
        res["kind"], res["kind"])
    out = [
        "\n=== 匯入完成 ===",
        f"  檔案：{res['file']}（格式：{kind}）",
        f"  發票 {res['invoices']} 筆、明細 {res['items']} 列入庫",
    ]
    if res["skipped"]:
        # 略過幾列一定要說。不說的話，欄位對不上的檔案會匯入成功卻只進一半
        out.append(f"  ！{res['rows']} 列裡有 {res['skipped']} 列找不到發票號碼，"
                   "已略過（頁尾或統計列的話屬正常）")
    if res.get("no_item_rows"):
        # 寬表是一列一品項，所以「有發票沒品項」多半是品名欄名對不上——
        # 不講的話，整份明細會靜默消失而摘要照樣說匯入完成
        out.append(f"  ！有 {res['no_item_rows']} 列解不出品項"
                   "（檔案本來就只有發票表頭的話屬正常；否則是品項欄名對不上，"
                   "→ 提供第一列的欄位名即可補上規則）")
    out.append("  → 接著跑 `twcrawl export` 重生報表就看得到。")
    return "\n".join(out)


def cmd_ingest(conn, ws: Workspace, *,
               capture_dir: Path | str | None = None) -> dict:
    root = Path(capture_dir) if capture_dir else ws.latest_capture()
    print(f"解析 {root}")
    return {"dir": str(root), **einvoice.ingest(root, conn)}


def cmd_handoff(ws: Workspace, *,
                capture_dir: Path | str | None = None) -> dict:
    root = Path(capture_dir) if capture_dir else ws.latest_capture()
    text = handoff.summarize(root)
    ws.ensure_out()
    out_path = ws.handoff_path(root.name)
    out_path.write_text(text, encoding="utf-8")
    return {"dir": str(root), "text": text, "path": str(out_path)}


def cmd_fda(conn, ws: Workspace, *, source: str = "all",
            url: str | None = None, since: str | None = None,
            max_pages: int = 500, headed: bool = False) -> dict:
    if url:
        selected = {"custom": url}
    elif source == "all":
        selected = dict(fda.SOURCES)
    else:
        selected = {source: fda.SOURCES[source]}

    results: dict[str, dict] = {}
    with browser_context(session_file=None, headed=headed) as ctx:
        page = ctx.new_page()
        for name, src_url in selected.items():
            print(f"\n=== 來源：{name} ===")
            # since 只給 feed 型來源：事件型清單不依日期排序，提前停止
            # 翻頁會任意截斷整份強制下架清單（見 fda.SOURCE_META）
            src_since = since if (since and fda.is_feed(name)) else None
            if since and not src_since:
                print(f"  （{name} 非 feed 型來源，不適用回溯日期，整份擷取）")
            results[name] = fda.fetch(page, conn, ws.out, url=src_url,
                                      max_pages=max_pages, since=src_since,
                                      name=name)
    return {"sources": results}


def cmd_match(conn, ws: Workspace, *, since: str | None = None) -> dict:
    return run_match(conn, ws.match_report, _classifier(ws), since=since)


def cmd_lottery(conn, ws: Workspace, *, fetch: bool = True,
                cloud: bool = True) -> dict:
    return lottery.run_lottery(conn, ws.cache, fetch=fetch, cloud=cloud)


def cmd_export(conn, ws: Workspace, *, open_browser: bool = True) -> dict:
    dash = export.write_export(conn, ws, _classifier(ws))
    if open_browser:
        open_dashboard(dash)
    return {"dashboard": str(dash)}


def cmd_backup(ws: Workspace, password: str, *,
               out_dir: Path | str | None = None) -> dict:
    path = backup_mod.make_backup(password, ws, out_dir=out_dir)
    return {"path": str(path)}


def db_stats(ws: Workspace) -> db.Stats:
    """打開工作區的資料庫數一數，順便證明它真的打得開。

    只回筆數與日期：金額與店家名是消費紀錄，不進終端機輸出
    （CLAUDE.md 個資界線）。這裡負責的是「開得開嗎」——查詢本身在 db.stats。
    """
    try:
        conn = db.connect(ws.db)
    except sqlite3.DatabaseError as e:
        raise SystemExit(
            f"資料庫打不開（{ws.db}）：{e}\n"
            "  → 檔案可能損毀，換一個備份包再試。") from None
    try:
        return db.stats(conn)
    finally:
        conn.close()  # Windows：開著的連線會擋住目錄清理


def cmd_restore(ws: Workspace, archive: Path | str, password: str, *,
                force: bool = False) -> dict:
    """還原備份包，並當場證明資料真的回來了。

    驗證不是選配：包解得開不等於資料庫可用，而這條路一年跑不到一次——
    沒有當場確認，等於還是沒演練過。
    """
    res = backup_mod.restore_backup(archive, password, ws, force=force)
    return {**res, "verify": db_stats(ws)}


def format_restore(res: dict) -> str:
    v: db.Stats = res["verify"]
    return "\n".join([
        "\n=== 還原完成 ===",
        f"  備份包：{res['archive']}",
        f"  還原 {res['files']} 個檔案（{res['bytes'] / 1_048_576:.1f} MB）"
        f"到 {res['root']}",
        f"  資料庫：{res['db']}",
        f"    發票 {v.invoices} 張、明細 {v.items} 列",
        f"    最新發票日期：{v.last or '（庫內沒有發票）'}",
        "  → 登入狀態不在備份包內（ADR-0001），"
        "要抓新資料請先 `twcrawl login`。",
        "  → 現在可以 `twcrawl export` 開儀表板確認。",
    ])


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


def update_steps(conn, ws: Workspace, *, login: bool = True,
                 backup: bool = True, cloud: bool = True,
                 open_browser: bool = True,
                 password: str | None = None,
                 today: _dt.date | None = None) -> list[Step]:
    """組出 update 的七步。

    抓取區間與 FDA 回溯日期在組裝時就算好（兩者都不受 login 影響），
    所以這個函式是純的：給定選項就決定了哪幾步會跑、標籤長什麼樣。
    """
    today = today or _dt.date.today()
    d_from, d_to = auto_month_range(db.stats(conn).last, today)
    since = (today - _dt.timedelta(days=FDA_LOOKBACK_DAYS)).isoformat()

    if not backup:
        backup_step = Step("backup", skip_reason="--no-backup")
    elif not password:
        backup_step = Step(
            "backup",
            skip_reason="沒有密碼來源（非互動且未設 TWCRAWL_BACKUP_PASSWORD）")
    else:
        backup_step = Step("backup", lambda: cmd_backup(ws, password))

    return [
        Step("login（請在瀏覽器完成登入）",
             lambda: cmd_login(ws),
             skip_reason="" if login else "--no-login"),
        Step(f"fetch {d_from} ～ {d_to}",
             lambda: cmd_fetch(conn, ws, d_from, d_to)),
        Step(f"fda（feed 型來源回溯至 {since}）",
             lambda: cmd_fda(conn, ws, since=since)),
        Step("match", lambda: cmd_match(conn, ws)),
        Step("lottery", lambda: cmd_lottery(conn, ws, cloud=cloud)),
        Step("export", lambda: cmd_export(conn, ws,
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
