"""twcrawl export — 從資料庫衍生儀表板資料檔並就位模板。

四頁制：`dashboard.html`（月報）＋ `query.html`（查詢頁）＋ `map.html`（地圖）＋
`fda.html`（食安頁），模板隨 repo 版控；`data.js`（衍生資料）gitignored、可隨時
重生。data.js 用 `<script src>` 載入，避開瀏覽器對 file:// 頁面不能 fetch 本機
JSON 的限制。品項與發票號碼是本機明文；載具號碼與 raw 永不進 data.js（ADR-0002）。
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import shutil
from collections import defaultdict
from pathlib import Path

from . import db
from .categories import Classifier, UNCATEGORIZED
from .workspace import Workspace

TEMPLATE = Path(__file__).parent / "web" / "dashboard.html"

# 食安頁的來源標示：已知來源的顯示名與型態（事件＝有專屬清單的單一食安案、
# 監測＝常設公告 feed）。未知來源（未來新事件）後備為「名稱原文＋事件」——
# 在 sites/fda.py 的 SOURCES 加一條，食安頁自動長出分頁，這裡不改也能動。
FDA_SOURCE_META: dict[str, tuple[str, str]] = {
    "edible_oil": ("中聯油脂案", "事件"),
    "csm_news": ("國內回收公告", "監測"),
    "csm_light": ("國際警訊", "監測"),
}


def _seller_info(conn, cl: Classifier, industries: dict[str, str]) -> dict[str, dict]:
    """法定店名 → 顯示名、店家分類、行業、地址。

    分類鏈（規則兩層 → 稅籍行業後備 → 未分類）整條在 Classifier 裡，
    這裡只接資料庫欄位。
    """
    reg = {ban: {"address": addr, "lat": lat, "lon": lon}
           for ban, addr, lat, lon in conn.execute(
               "select ban, address, lat, lon from biz_registry")}
    info: dict[str, dict] = {}
    for sname, ban in conn.execute(
            "select seller_name, max(seller_ban) from invoices "
            "where seller_name is not null group by seller_name"):
        cat = cl.for_seller(sname)
        r = reg.get(str(ban).strip() if ban else "")
        info[sname] = {
            "display": cl.display_name(sname),
            "legal": sname, "category": cat.name, "source": cat.source,
            "industry": industries.get(sname),
            "address": r["address"] if r else None,
            "lat": r["lat"] if r else None,
            "lon": r["lon"] if r else None,
        }
    return info


def _top_items(conn, k: int = 3) -> dict[str, list[str]]:
    counts: dict[str, defaultdict] = {}
    for sname, desc, n in conn.execute(
            "select v.seller_name, i.description, count(*) "
            "from invoice_items i join invoices v on v.inv_num = i.inv_num "
            "where i.description is not null and v.seller_name is not null "
            "group by v.seller_name, i.description"):
        counts.setdefault(sname, defaultdict(int))[str(desc)] += n
    return {s: [d for d, _ in sorted(c.items(), key=lambda kv: -kv[1])[:k]]
            for s, c in counts.items()}


def _items_by_invoice(conn) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for inv_num, desc, qty, price, amount in conn.execute(
            "select inv_num, description, quantity, unit_price, amount "
            "from invoice_items order by inv_num, row_no"):
        out.setdefault(inv_num, []).append(
            {"desc": desc, "qty": qty, "price": price, "amount": amount})
    return out


def _detect_fixed(inv_rows: list[dict]) -> list[dict]:
    """固定支出偵測（CONTEXT.md：同店家、金額相近、月級以上週期、至少 3 次）。

    月報磚與查詢頁固定支出視圖共用這份結果；偵測是提示，不代表存在契約。
    """
    if not inv_rows:
        return []
    by_seller: dict[str, list[dict]] = {}
    for v in inv_rows:
        by_seller.setdefault(v["seller"], []).append(v)
    data_max = _dt.date.fromisoformat(max(v["date"] for v in inv_rows))
    found: list[dict] = []
    for seller, rows in by_seller.items():
        clusters: list[dict] = []   # 金額相近（±max(15, 5%)）的貪婪分群
        for v in sorted(rows, key=lambda r: r["amount"]):
            c = clusters[-1] if clusters else None
            if c and v["amount"] - c["mean"] <= max(15, c["mean"] * 0.05):
                c["rows"].append(v)
                c["mean"] = sum(r["amount"] for r in c["rows"]) / len(c["rows"])
            else:
                clusters.append({"rows": [v], "mean": v["amount"]})
        for c in clusters:
            if len(c["rows"]) < 3:
                continue
            ds = sorted(_dt.date.fromisoformat(r["date"]) for r in c["rows"])
            iv = [(b - a).days for a, b in zip(ds, ds[1:])]
            s = sorted(iv)
            n = len(s)
            med = s[(n - 1) // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
            if med < 25 or med > 400:   # 月級以上才算；高頻日常歸非必要消費管
                continue
            tol = max(5, med * 0.25)
            outliers = sum(1 for i in iv if abs(i - med) > tol)
            if outliers > (1 if len(iv) >= 4 else 0):
                continue
            last = ds[-1]
            active = (data_max - last).days <= med * 1.6
            found.append({
                "seller": seller, "amount": c["mean"], "periodDays": med,
                "n": len(c["rows"]),
                "first": ds[0].isoformat(), "last": last.isoformat(),
                "next": (last + _dt.timedelta(days=med)).isoformat()
                        if active else None,
                "active": active, "monthly": c["mean"] * 30.44 / med,
            })
    return sorted(found, key=lambda x: (not x["active"], -x["monthly"]))


def build_payload(conn, ws: Workspace, classifier: Classifier) -> dict:
    """衍生儀表板資料。需要工作區的兩條路徑：比對報告與雲端獎清冊快取。"""
    # 稅籍行業後備一律在這裡接上：Classifier 可能是在拿到 conn 之前建的
    # （serve、測試都是），少接不會報錯、只會讓兩成店家靜默掉回未分類。
    industries = db.seller_industries(conn)
    cl = classifier.with_industries(industries)
    info = _seller_info(conn, cl, industries)
    top_items = _top_items(conn)
    invs = conn.execute(
        "select inv_num, inv_date, seller_name, amount from invoices "
        "where inv_date is not null and amount is not null order by inv_date"
    ).fetchall()

    items = _items_by_invoice(conn)
    months: dict[str, dict] = {}
    cats: dict[str, dict] = {}
    sellers: dict[str, dict] = {}
    unnecessary: list[dict] = []
    invoice_rows: list[dict] = []

    for num, inv_date, seller, amount in invs:
        m = str(inv_date)[:7]
        si = info.get(seller) or {"display": seller or "（無店名）",
                                  "legal": seller, "category": UNCATEGORIZED,
                                  "industry": None, "address": None,
                                  "lat": None, "lon": None}
        inv_items = items.get(num, [])
        # 品項覆寫（發票層級）：跨業態店家（好市多加油站）靠品項關鍵字改
        # 單張發票的分類；店家本身的業態分類（sellers、地圖）不動
        cat = cl.for_invoice(seller, (i["desc"] for i in inv_items))
        mon = months.setdefault(m, {"month": m, "total": 0.0, "count": 0,
                                    "byCategory": defaultdict(float)})
        mon["total"] += amount
        mon["count"] += 1
        mon["byCategory"][cat.name] += amount
        c = cats.setdefault(cat.name, {"name": cat.name, "total": 0.0, "count": 0,
                                       "unnecessary": cat.unnecessary})
        c["total"] += amount
        c["count"] += 1
        s = sellers.setdefault(seller, {
            "name": si["display"], "category": si["category"],
            "total": 0.0, "count": 0,
            "legal": si["legal"] if si["display"] != si["legal"] else None,
            "industry": si["industry"], "address": si["address"],
            "lat": si["lat"], "lon": si["lon"],
            "topItems": top_items.get(seller) or [],
        })
        s["total"] += amount
        s["count"] += 1
        if cat.unnecessary:
            unnecessary.append({"date": str(inv_date)[:10], "seller": si["display"],
                                "category": cat.name, "amount": amount})
        invoice_rows.append({"num": num, "date": str(inv_date)[:10],
                             "seller": si["display"], "category": cat.name,
                             "amount": amount, "items": inv_items})

    for mon in months.values():
        mon["byCategory"] = dict(mon["byCategory"])

    fda_rows = conn.execute("select count(*) from fda_rows").fetchone()[0]

    # 來源歸戶：match 報告的 source 欄是 source_url，反查 SOURCES 得名稱；
    # 直接寫名稱的（測試、未來格式）也照樣解析
    from .sites.fda import SOURCES as _FDA_SOURCES  # 延後載入：fda 會拖 playwright
    url2name = {url: name for name, url in _FDA_SOURCES.items()}

    def src_info(src: str) -> tuple[str, str, str]:
        key = url2name.get(src, src or "")
        label, kind = FDA_SOURCE_META.get(key, (key or "未標來源", "事件"))
        return key, label, kind

    sources: dict[str, dict] = {}

    def src_bucket(src: str) -> dict:
        key, label, kind = src_info(src)
        return sources.setdefault(key, {"key": key, "label": label, "kind": kind,
                                        "rows": 0, "lastSeen": None, "hits": 0})

    for url, cnt, last in conn.execute(
            "select source_url, count(*), max(last_seen) from fda_rows "
            "group by source_url"):
        s = src_bucket(url or "")
        s["rows"] += cnt
        s["lastSeen"] = max(s["lastSeen"] or "", str(last or "")[:10]) or None

    report = ws.match_report
    match_counts: dict[str, int] | None = None
    match_rows: list[dict] | None = None
    if report.exists():
        match_counts = defaultdict(int)
        match_rows = []
        with report.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                match_counts[row.get("level", "?")] += 1
                s = src_bucket(row.get("source") or "")
                s["hits"] += 1
                match_rows.append({
                    "level": row.get("level"), "date": row.get("inv_date"),
                    "num": row.get("inv_num"),
                    "invoice": row.get("invoice_side"),
                    "fda": row.get("fda_side"), "source": s["key"],
                })
        match_counts = dict(match_counts)

    # 對獎：號碼已在 lottery_draws（lottery 指令維護），這裡即算即得。
    # data.js 只放中獎結果與期別摘要，不放中獎號碼本身（UI 用不到）。
    from . import lottery as lottery_mod
    lot_raw = lottery_mod.check_invoices(conn, ws.cache)
    lottery = {
        "uncovered": lot_raw["uncovered"],
        "periods": [
            {"period": p["period"],
             "label": lottery_mod.period_label(p["period"]),
             "months": p["months"],
             "claimStart": p["claim_start"], "claimEnd": p["claim_end"],
             "nInvoices": p["n_invoices"],
             "wins": [
                 {"num": w["inv_num"], "date": w["date"],
                  "seller": (info.get(w["seller"]) or {}).get(
                      "display", w["seller"]),
                  "prize": w["prize"], "prizeAmount": w["prize_amount"],
                  "also": w.get("also"),
                  "claimEnd": p["claim_end"],
                  "periodLabel": lottery_mod.period_label(p["period"])}
                 for w in p["wins"]]}
            for p in lot_raw["periods"]],
    }
    if lot_raw["periods"]:
        np_, nd = lottery_mod.next_draw(lot_raw["periods"][0]["period"])
        pending = conn.execute(
            "select count(*) from invoices where substr(inv_date, 1, 7) in (?, ?)",
            lottery_mod.period_months(np_)).fetchone()[0]
        lottery["next"] = {"label": lottery_mod.period_label(np_),
                          "drawDate": nd, "pending": pending}

    # 「沒有任何規則命中」看 source，不是拿名字跟 UNCATEGORIZED 比
    uncategorized = sorted(
        (s for name, s in sellers.items()
         if (info.get(name) or {}).get("source", "none") == "none"),
        key=lambda s: -s["total"],
    )[:15]

    return {
        "generatedAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "invoiceCount": len(invs),
        "invoices": invoice_rows,
        "fixed": _detect_fixed(invoice_rows),
        "months": [months[k] for k in sorted(months)],
        "categories": sorted(cats.values(), key=lambda c: -c["total"]),
        "sellers": sorted(sellers.values(), key=lambda s: -s["total"]),
        "unnecessary": sorted(unnecessary, key=lambda u: u["date"], reverse=True),
        "uncategorized": uncategorized,
        "fda": {"rows": fda_rows, "match": match_counts, "matches": match_rows,
                "sources": sorted(sources.values(),
                                  key=lambda s: (s["kind"] != "事件", s["label"]))},
        "lottery": lottery,
    }


def write_export(conn, ws: Workspace, classifier: Classifier,
                 template: Path = TEMPLATE) -> Path:
    out_dir = ws.ensure_out()
    payload = build_payload(conn, ws, classifier)
    data_js = out_dir / "data.js"
    data_js.write_text(
        "window.TWCRAWL_DATA = "
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + ";\n",
        encoding="utf-8",
    )
    dash = out_dir / "dashboard.html"
    shutil.copyfile(template, dash)
    web = template.parent
    for page in ("query.html", "map.html", "fda.html"):
        if (web / page).exists():
            shutil.copyfile(web / page, out_dir / page)
    if (web / "vendor").exists():
        (out_dir / "vendor").mkdir(exist_ok=True)
        for v in ("leaflet.js", "leaflet.css"):
            shutil.copyfile(web / "vendor" / v, out_dir / "vendor" / v)
    n_unc = len(payload["uncategorized"])
    print(f"儀表板：{dash}（發票 {payload['invoiceCount']} 張、"
          f"{len(payload['months'])} 個月）")
    if n_unc:
        print(f"！有未分類店家（金額前 {n_unc} 名列在儀表板底部）"
              f"——想歸類就加進 categories.local.json")
    return dash
