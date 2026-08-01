"""twcrawl — 比對食藥署問題商品清單與你自己的電子發票消費紀錄。

這一層只做三件事：解析 argv、把參數交給 commands 的指令函式、把回傳
結果排版印出來。指令的實際內容在 commands.py——`twcrawl <cmd>` 與
`twcrawl update` 的第 N 步呼叫的是同一個函式，接線不會兩邊各打一遍。
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from . import bizreg, commands, db, geocode
from . import lottery as lottery_mod
from . import match as match_mod
from .browser import browser_context
from .probe import probe as run_probe
from .sites import fda
from .workspace import Workspace


@contextlib.contextmanager
def _db(path):
    """指令用完就關——Windows 上開著的連線會擋住暫存目錄清理。"""
    conn = db.connect(path)
    try:
        yield conn
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    # 只取摘要那一行；後面幾段是給讀原始碼的人看的，不該倒進 --help
    ap = argparse.ArgumentParser(prog="twcrawl",
                                 description=(__doc__ or "").split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="開啟瀏覽器，由人工完成電子發票平台登入並保存 session")
    p_login.add_argument("--slow-mo", type=int, default=0)

    sub.add_parser("capture", help="開啟平台並錄下查詢／匯出的回應（人工操作）")

    p_fetch = sub.add_parser(
        "fetch", help="登入後逐月自動抓取指定區間的發票（不需人工操作瀏覽器）"
    )
    p_fetch.add_argument("--from", dest="date_from", required=True, help="起始月份 YYYY-MM")
    p_fetch.add_argument("--to", dest="date_to", required=True, help="結束月份 YYYY-MM")
    p_fetch.add_argument(
        "--no-details", action="store_true", help="只抓發票表頭，不逐張抓品項明細（較快）"
    )
    p_fetch.add_argument("--headed", action="store_true", help="顯示瀏覽器視窗（除錯用）")

    p_ing = sub.add_parser("ingest", help="把擷取到的資料解析後寫入 SQLite")
    p_ing.add_argument("--dir", help="擷取目錄，預設為最新一次")

    p_hand = sub.add_parser(
        "handoff", help="產生可安全分享的擷取摘要（所有值代換成型別名稱）"
    )
    p_hand.add_argument("--dir", help="擷取目錄，預設為最新一次")

    p_fda = sub.add_parser("fda", help="擷取食藥署問題商品清單（下架、回收公告、警訊）")
    p_fda.add_argument("--url", help="自訂網址（優先於 --source）")
    p_fda.add_argument(
        "--source", default="all", choices=["all", *fda.SOURCES],
        help="要擷取的來源，預設全部",
    )
    p_fda.add_argument(
        "--since",
        help="feed 型來源（回收公告、國際警訊）回溯至此日期（YYYY-MM-DD）即停止翻頁；"
             "事件型清單不適用，一律整份擷取",
    )
    p_fda.add_argument("--max-pages", type=int, default=500)
    p_fda.add_argument("--headed", action="store_true", help="顯示瀏覽器視窗（除錯用）")

    p_match = sub.add_parser(
        "match", help="比對發票與 FDA 問題商品（店家、品項、警訊標題三層級）"
    )
    p_match.add_argument("--since", help="只比對此日期（YYYY-MM-DD）之後的發票")

    p_lot = sub.add_parser(
        "lottery", help="對獎：抓官方中獎號碼（本期＋上期）比對庫內發票"
    )
    p_lot.add_argument(
        "--offline", action="store_true", help="不連網，用資料庫既有號碼對"
    )
    p_lot.add_argument(
        "--no-cloud", action="store_true",
        help="跳過雲端專屬獎清冊更新（五百元獎首次下載約 119MB，之後走快取）"
    )

    p_exp = sub.add_parser(
        "export",
        help="從資料庫衍生五頁（月報、查詢、食安、年度回顧、地圖 + data.js）",
    )
    p_exp.add_argument("--no-open", action="store_true", help="產出後不自動開啟儀表板")

    p_srv = sub.add_parser(
        "serve",
        help="本機小站（只綁 127.0.0.1）：同一套頁面＋分類寫回 categories.local.json（ADR-0002）",
    )
    p_srv.add_argument("--port", type=int, default=8765)
    p_srv.add_argument("--no-open", action="store_true", help="不自動開瀏覽器")

    p_biz = sub.add_parser(
        "bizreg", help="下載財政部稅籍登記，建立本機統編→行業/地址對照表（公開資料）"
    )
    p_biz.add_argument("--force", action="store_true", help="重新下載（官方每月更新）")

    sub.add_parser(
        "geocode", help="把稅籍營業地址批次轉座標（OSM Nominatim，供地圖檢視；增量）"
    )

    p_bak = sub.add_parser(
        "backup", help="產生 AES-256 加密備份包（僅密文可上雲，見 docs/adr/0001）"
    )
    p_bak.add_argument("--out", default=None,
                       help="備份包輸出目錄（預設工作區的 out/backup；"
                            "可指向工作區外，例如雲端同步目錄）")

    p_upd = sub.add_parser(
        "update",
        help="每月例行：登入→fetch→fda→match→lottery→export→backup 一氣呵成",
    )
    p_upd.add_argument("--no-login", action="store_true", help="沿用現有 session，跳過登入")
    p_upd.add_argument("--no-backup", action="store_true", help="不產生備份包")
    p_upd.add_argument("--no-open", action="store_true", help="export 後不自動開啟儀表板")
    p_upd.add_argument(
        "--no-cloud", action="store_true",
        help="對獎時跳過雲端專屬獎清冊更新（五百元獎首次下載約 119MB）"
    )

    p_probe = sub.add_parser("probe", help="產生頁面結構偵察報告")
    p_probe.add_argument("url")
    p_probe.add_argument("--session", help="要沿用的登入 session 名稱，例如 einvoice")
    p_probe.add_argument("--headed", action="store_true")

    return ap


def _dispatch(args: argparse.Namespace, ws: Workspace) -> int:
    if args.cmd == "login":
        commands.cmd_login(ws, slow_mo=args.slow_mo)
        return 0

    if args.cmd == "capture":
        with _db(ws.db) as conn:
            commands.cmd_capture(conn, ws)
        return 0

    if args.cmd == "fetch":
        with _db(ws.db) as conn:
            commands.cmd_fetch(conn, ws, args.date_from, args.date_to,
                               with_details=not args.no_details,
                               headed=args.headed)
        return 0

    if args.cmd == "ingest":
        with _db(ws.db) as conn:
            commands.cmd_ingest(conn, ws, capture_dir=args.dir)
        return 0

    if args.cmd == "handoff":
        res = commands.cmd_handoff(ws, capture_dir=args.dir)
        print(res["text"])
        print(f"\n（已同步存到 {res['path']}）")
        return 0

    if args.cmd == "fda":
        with _db(ws.db) as conn:
            commands.cmd_fda(conn, ws, source=args.source, url=args.url,
                             since=args.since, max_pages=args.max_pages,
                             headed=args.headed)
        return 0

    if args.cmd == "match":
        ws.require_db()
        with _db(ws.db) as conn:
            res = commands.cmd_match(conn, ws, since=args.since)
        print(match_mod.format_result(res))
        return 0

    if args.cmd == "lottery":
        ws.require_db()
        print("=== 統一發票對獎 ===")
        with _db(ws.db) as conn:
            res = commands.cmd_lottery(conn, ws, fetch=not args.offline,
                                       cloud=not args.no_cloud)
        print(lottery_mod.format_result(res))
        return 0

    if args.cmd == "export":
        ws.require_db()
        with _db(ws.db) as conn:
            commands.cmd_export(conn, ws, open_browser=not args.no_open)
        return 0

    if args.cmd == "backup":
        ws.require_db()
        pw = commands.backup_password()
        if not pw:
            raise SystemExit("需要備份密碼（互動輸入或設 TWCRAWL_BACKUP_PASSWORD）。")
        commands.cmd_backup(pw, ws, out_dir=args.out)
        return 0

    if args.cmd == "update":
        # 密碼在組裝步驟時就問，不是跑了一小時之後才卡在提示上
        pw = None if args.no_backup else commands.backup_password()
        with _db(ws.db) as conn:
            steps = commands.update_steps(
                conn, ws,
                login=not args.no_login, backup=not args.no_backup,
                cloud=not args.no_cloud, open_browser=not args.no_open,
                password=pw,
            )
            summary = commands.run_steps(steps)
        print(commands.format_update(summary))
        return 1 if summary["failed"] else 0

    # 以下是純委派：包一層不會讓任何複雜度集中，所以直接呼叫底層
    if args.cmd == "serve":
        ws.require_db()
        from . import serve as serve_mod
        serve_mod.serve(ws, port=args.port, open_browser=not args.no_open)
        return 0

    if args.cmd == "bizreg":
        with _db(ws.db) as conn:
            bizreg.refresh(conn, ws.bizreg_cache, force=args.force)
        return 0

    if args.cmd == "geocode":
        ws.require_db()
        with _db(ws.db) as conn:
            geocode.refresh(conn)
        return 0

    if args.cmd == "probe":
        session_file = ws.state_path(args.session) if args.session else None
        with browser_context(session_file=session_file,
                             headed=args.headed) as ctx:
            run_probe(ctx.new_page(), args.url, ws.probe_out)
        return 0

    return 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # 工作區＝目前目錄；所有路徑由它推出（CONTEXT.md「工作區」）
    ws = Workspace(Path.cwd())
    try:
        return _dispatch(args, ws)
    except KeyboardInterrupt:
        # browser.wait_for_operator 的人工中止也走這條
        print("\n已中止。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
