"""twcrawl — 比對食藥署問題商品清單與你自己的電子發票消費紀錄。"""

from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import os
import sys
from pathlib import Path

from . import backup as backup_mod
from . import bizreg, db, export, geocode, handoff, lottery
from .browser import browser_context
from .match import run_match
from .netcapture import Capture
from .probe import probe as run_probe
from .sites import einvoice, einvoice_fetch, fda


def _latest_capture() -> Path:
    roots = sorted(Path("captures").glob("einvoice-*"), reverse=True)
    if not roots:
        raise SystemExit("找不到任何擷取結果，請先執行 `einvoice capture`。")
    return roots[0]


def _auto_month_range(last_inv_date: str | None, today: _dt.date) -> tuple[str, str]:
    """update 的抓取區間：從資料庫最新發票的月份（重抓補漏，upsert 冪等）
    到當月；資料庫是空的就回推 5 個月（明細保存期內）。"""
    to = f"{today:%Y-%m}"
    if last_inv_date:
        return str(last_inv_date)[:7], to
    y, m = today.year, today.month - 5
    if m <= 0:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}", to


def _backup_password() -> str | None:
    pw = os.environ.get("TWCRAWL_BACKUP_PASSWORD")
    if pw:
        return pw
    if not sys.stdin or not sys.stdin.isatty():
        return None
    return getpass.getpass("備份密碼（留空跳過備份）：")


def _open_dashboard(path: Path) -> None:
    """export 完直接開瀏覽器看（file:// 檢視是日常路徑，見 docs/adr/0002）。"""
    import webbrowser
    try:
        webbrowser.open(path.resolve().as_uri())
    except Exception as e:  # 開不了瀏覽器不該讓整個指令失敗
        print(f"！無法自動開啟儀表板（{e}），請自行開 {path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="twcrawl", description=__doc__)
    ap.add_argument("--db", default=str(db.DEFAULT_DB), help="SQLite 檔案路徑")
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
    p_fda.add_argument("--since", help="新聞式清單回溯至此日期（YYYY-MM-DD）即停止翻頁")
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
        help="從資料庫衍生四頁（月報、查詢、食安、地圖 + data.js）",
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
    p_bak.add_argument("--out", default=str(backup_mod.BACKUP_DIR), help="備份包輸出目錄")

    p_upd = sub.add_parser(
        "update", help="每月例行：登入→fetch→fda→match→export（＋備份包）一氣呵成"
    )
    p_upd.add_argument("--no-login", action="store_true", help="沿用現有 session，跳過登入")
    p_upd.add_argument("--no-backup", action="store_true", help="不產生備份包")
    p_upd.add_argument("--no-open", action="store_true", help="export 後不自動開啟儀表板")

    p_probe = sub.add_parser("probe", help="產生頁面結構偵察報告")
    p_probe.add_argument("url")
    p_probe.add_argument("--session", help="要沿用的登入 session 名稱，例如 einvoice")
    p_probe.add_argument("--headed", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "login":
        with browser_context(session=einvoice.SESSION, headed=True, slow_mo=args.slow_mo) as ctx:
            einvoice.login(ctx)
        return 0

    if args.cmd == "capture":
        cap = Capture("einvoice")
        with browser_context(session=einvoice.SESSION, headed=True) as ctx:
            root = einvoice.capture(ctx, cap)
        conn = db.connect(args.db)
        einvoice.ingest(root, conn)
        return 0

    if args.cmd == "fetch":
        if not Path("state/einvoice.json").exists():
            raise SystemExit("找不到登入狀態，請先執行 `twcrawl login`。")
        conn = db.connect(args.db)
        with browser_context(session=einvoice.SESSION, headed=args.headed) as ctx:
            einvoice_fetch.fetch_range(
                ctx, conn, args.date_from, args.date_to,
                with_details=not args.no_details,
            )
        return 0

    if args.cmd == "ingest":
        root = Path(args.dir) if args.dir else _latest_capture()
        print(f"解析 {root}")
        conn = db.connect(args.db)
        einvoice.ingest(root, conn)
        return 0

    if args.cmd == "handoff":
        root = Path(args.dir) if args.dir else _latest_capture()
        text = handoff.summarize(root)
        print(text)
        out_path = Path("out") / f"handoff_{root.name}.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"\n（已同步存到 {out_path}）")
        return 0

    if args.cmd == "fda":
        conn = db.connect(args.db)
        if args.url:
            selected = {"custom": args.url}
        elif args.source == "all":
            selected = dict(fda.SOURCES)
        else:
            selected = {args.source: fda.SOURCES[args.source]}
        with browser_context(session=None, headed=args.headed) as ctx:
            page = ctx.new_page()
            for name, url in selected.items():
                print(f"\n=== 來源：{name} ===")
                fda.fetch(page, conn, url=url, max_pages=args.max_pages,
                          since=args.since, name=name)
        return 0

    if args.cmd == "match":
        conn = db.connect(args.db)
        run_match(conn, since=args.since)
        return 0

    if args.cmd == "lottery":
        conn = db.connect(args.db)
        lottery.run_lottery(conn, fetch=not args.offline,
                            cloud=not args.no_cloud)
        return 0

    if args.cmd == "export":
        conn = db.connect(args.db)
        dash = export.write_export(conn)
        if not args.no_open:
            _open_dashboard(dash)
        return 0

    if args.cmd == "serve":
        from . import serve as serve_mod
        serve_mod.serve(args.db, port=args.port, open_browser=not args.no_open)
        return 0

    if args.cmd == "bizreg":
        conn = db.connect(args.db)
        bizreg.refresh(conn, force=args.force)
        return 0

    if args.cmd == "geocode":
        conn = db.connect(args.db)
        geocode.refresh(conn)
        return 0

    if args.cmd == "backup":
        pw = _backup_password()
        if not pw:
            raise SystemExit("需要備份密碼（互動輸入或設 TWCRAWL_BACKUP_PASSWORD）。")
        backup_mod.make_backup(pw, db_path=args.db, out_dir=args.out)
        return 0

    if args.cmd == "update":
        conn = db.connect(args.db)
        if not args.no_login:
            print("=== 1/7 login（請在瀏覽器完成登入） ===")
            with browser_context(session=einvoice.SESSION, headed=True) as ctx:
                einvoice.login(ctx)
        last = conn.execute("select max(inv_date) from invoices").fetchone()[0]
        d_from, d_to = _auto_month_range(last, _dt.date.today())
        print(f"=== 2/7 fetch {d_from} ～ {d_to} ===")
        with browser_context(session=einvoice.SESSION, headed=False) as ctx:
            einvoice_fetch.fetch_range(ctx, conn, d_from, d_to, with_details=True)
        since = (_dt.date.today() - _dt.timedelta(days=90)).isoformat()
        print(f"=== 3/7 fda（回溯至 {since}） ===")
        with browser_context(session=None, headed=False) as ctx:
            page = ctx.new_page()
            for name, url in fda.SOURCES.items():
                print(f"--- 來源：{name} ---")
                fda.fetch(page, conn, url=url, max_pages=500, since=since, name=name)
        print("=== 4/7 match ===")
        run_match(conn)
        print("=== 5/7 lottery ===")
        lottery.run_lottery(conn)
        print("=== 6/7 export ===")
        dash = export.write_export(conn)
        if not args.no_open:
            _open_dashboard(dash)
        if args.no_backup:
            print("=== 7/7 backup（已依 --no-backup 跳過） ===")
            return 0
        print("=== 7/7 backup ===")
        pw = _backup_password()
        if pw:
            backup_mod.make_backup(pw, db_path=args.db)
        else:
            print("！沒有密碼來源（非互動且未設 TWCRAWL_BACKUP_PASSWORD），跳過備份。")
        return 0

    if args.cmd == "probe":
        with browser_context(session=args.session, headed=args.headed) as ctx:
            run_probe(ctx.new_page(), args.url)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
