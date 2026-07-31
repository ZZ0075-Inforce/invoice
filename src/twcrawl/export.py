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

# 四頁模板與共用資產（ui.css）的來源目錄；package-relative，不隨工作區走
WEB_DIR = Path(__file__).parent / "web"

# 食安頁的來源標示（顯示名與事件／監測型態）住在 sites/fda.py 的 SOURCE_META
# ——抓取端要靠型態決定 since 適不適用，呈現端要靠它分頁，同一份知識兩邊都要。
# 在 fda.py 的 SOURCES 加一條，食安頁自動長出分頁，這裡不改也能動。


def _seller_info(conn, cl: Classifier, industries: dict[str, str]) -> dict[str, dict]:
    """法定店名 → 顯示名、店家分類、行業、地址。

    分類鏈（規則兩層 → 稅籍行業後備 → 未分類）整條在 Classifier 裡，
    這裡只接資料庫欄位。
    """
    reg = {b.ban: b for b in db.biz_registry(conn)}
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
            "address": r.address if r else None,
            "lat": r.lat if r else None,
            "lon": r.lon if r else None,
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
    for it in db.invoice_items(conn):
        out.setdefault(it.inv_num, []).append(
            {"desc": it.desc, "qty": it.qty, "price": it.price,
             "amount": it.amount})
    return out


# 分類色槽：6 槽對應 ui.css 的 --s1..--s6。指派由匯出端決定並持久化在
# 工作區本機狀態（state/catslots.json），頁面只讀 categories[].slot——
# 以前由 JS 依當期金額排名取前六，排名一變整組跳色，前後兩份月報無法
# 對照（issue #10）。未分類永遠不佔槽（中性灰）。
N_SLOTS = 6


def assign_slots(prev: dict[str, int], ranked: list[str]) -> dict[str, int]:
    """色槽指派：在榜者沿用舊槽、新進者取最小空槽、槽滿時向落榜持有者收回。

    穩定性契約：輸入不變則輸出不變；排名變動不改既有持有者的槽；收回只
    發生在「新進者要槽而沒有空槽」時，且從最不需要的持有者開始（不在
    ranked 裡的先收，再收名次最低的）。回傳含未在榜但仍持槽者——分類
    短暫消失（該期零消費）再回來時拿回同一色。
    """
    top = ranked[:N_SLOTS]
    out = dict(prev)
    for name in top:
        if name in out:
            continue
        free = sorted(set(range(1, N_SLOTS + 1)) - set(out.values()))
        if free:
            out[name] = free[0]
            continue
        fallen = [n for n in out if n not in top]
        if not fallen:      # 理論上到不了：top 與持有者都不超過 N_SLOTS
            break
        worst = max(fallen, key=lambda n: (ranked.index(n) if n in ranked
                                           else len(ranked)))
        out[name] = out.pop(worst)
    return out


def _load_slots(path: Path) -> dict[str, int]:
    """讀回上次的指派。壞檔（手改壞、槽號超界或重複、bool 冒充槽號、
    未分類佔槽）整份放棄重指派——色槽只是外觀延續性，不值得為它中斷
    匯出；「未分類永遠中性灰」則是不變式，不能被手改的狀態檔繞過。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        out: dict[str, int] = {}
        for name, slot in raw.items():
            if (not isinstance(name, str) or name == UNCATEGORIZED
                    or not isinstance(slot, int) or isinstance(slot, bool)
                    or not 1 <= slot <= N_SLOTS or slot in out.values()):
                return {}
            out[name] = slot
        return out
    except (OSError, ValueError, AttributeError):
        return {}


# 發票狀態碼→中文（issue #14）。PRD 記載的來源（capture 到的 com001i/
# statusCodes 字典）實測只有 UI 訊息碼、沒有發票狀態碼，所以這份對照只收
# 實測可對應的碼：庫內全部正常開立發票都帶 INVOICE0003S。未收錄的碼原樣
# 進 payload——查詢頁顯示原始碼不吞資訊，新碼出現時在這裡補一行即可。
INVOICE_STATUS_ZH = {"INVOICE0003S": "開立"}


def load_budget(path: Path) -> dict | None:
    """讀預算設定（budget.local.json）；沒有檔或空設定回 None → payload 無
    budget 區塊 → 儀表板無磚。

    預算金額是個資（ADR-0001）：檔案在 .gitignore、錯誤訊息只印鍵名不印值。
    防呆比照 categories.load_local_config——手貼 JSON 壞掉時給人話而非
    traceback，重複鍵也一樣擋（JSON 的後者會悄悄蓋掉前者）。只有兩個合法
    鍵，打錯字（montly）若被靜默忽略，磚會無聲消失，所以不認得的鍵直接報錯。
    """
    if not path.exists():
        return None

    def no_dupes(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise SystemExit(
                    f"{path} 裡「{k}」重複出現——JSON 的後者會悄悄蓋掉前者，"
                    "請留一筆再重跑。")
            d[k] = v
        return d

    try:
        cfg = json.loads(path.read_text(encoding="utf-8"),
                         object_pairs_hook=no_dupes)
    except SystemExit:
        raise
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"{path} 第 {e.lineno} 行第 {e.colno} 欄有語法錯誤：{e.msg}\n"
            "常見原因：多了／少了逗號、引號沒關。正確格式範例：\n"
            '{ "monthly": 25000, "unnecessary": 3000 }') from None
    if not isinstance(cfg, dict):
        raise SystemExit(f"{path} 最外層要是物件（{{...}}）。")
    unknown = set(cfg) - {"monthly", "unnecessary"}
    if unknown:
        raise SystemExit(
            f"{path} 有不認得的鍵：{'、'.join(sorted(unknown))}——只收 "
            "monthly（每月總額）與 unnecessary（非必要消費上限）。")
    out: dict[str, float | None] = {}
    for key, label in (("monthly", "每月總額"),
                       ("unnecessary", "非必要消費上限")):
        v = cfg.get(key)
        if v is None:
            out[key] = None
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise SystemExit(f"{path} 的 {key}（{label}）要是正數金額。")
        else:
            out[key] = float(v)
    return out if any(v is not None for v in out.values()) else None


# 固定支出的判準。查詢頁把這些數字寫進說明文案，所以由 payload 帶過去——
# 以前門檻在這裡、文案在 query.html，改了門檻文案不會跟著改。
FIXED_RULE = {
    "minCount": 3,        # 至少出現幾次才算規律
    "tolAbs": 15,         # 金額相近：±max(tolAbs 元, tolPct%)
    "tolPct": 5,
    "minDays": 25,        # 週期下限——高頻日常（手搖飲）歸非必要消費管
    "maxDays": 400,       # 週期上限
    "staleFactor": 1.6,   # 超過幾個週期沒再出現就算疑似已停
}

_PERIOD_BANDS = ((38, "每月"), (70, "每兩月"), (100, "每季"),
                 (200, "每半年"), (FIXED_RULE["maxDays"], "每年"))


def _period_label(days: float) -> str:
    for limit, label in _PERIOD_BANDS:
        if days <= limit:
            return label
    return f"約 {round(days)} 天"   # med 已被 maxDays 濾過，實務上到不了


def _detect_fixed(inv_rows: list[dict]) -> list[dict]:
    """固定支出偵測（CONTEXT.md：同店家、金額相近、月級以上週期、至少 3 次）。

    月報磚與查詢頁固定支出視圖共用這份結果；偵測是提示，不代表存在契約。
    週期標籤在這裡算完——門檻是這裡的實作細節，讓頁面重述一次就會漂。
    """
    if not inv_rows:
        return []
    by_seller: dict[str, list[dict]] = {}
    for v in inv_rows:
        by_seller.setdefault(v["seller"], []).append(v)
    data_max = _dt.date.fromisoformat(max(v["date"] for v in inv_rows))
    found: list[dict] = []
    for seller, rows in by_seller.items():
        clusters: list[dict] = []   # 金額相近的貪婪分群
        for v in sorted(rows, key=lambda r: r["amount"]):
            c = clusters[-1] if clusters else None
            tol_amt = max(FIXED_RULE["tolAbs"],
                          c["mean"] * FIXED_RULE["tolPct"] / 100) if c else 0
            if c and v["amount"] - c["mean"] <= tol_amt:
                c["rows"].append(v)
                c["mean"] = sum(r["amount"] for r in c["rows"]) / len(c["rows"])
            else:
                clusters.append({"rows": [v], "mean": v["amount"]})
        for c in clusters:
            if len(c["rows"]) < FIXED_RULE["minCount"]:
                continue
            ds = sorted(_dt.date.fromisoformat(r["date"]) for r in c["rows"])
            iv = [(b - a).days for a, b in zip(ds, ds[1:])]
            s = sorted(iv)
            n = len(s)
            med = s[(n - 1) // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
            if not FIXED_RULE["minDays"] <= med <= FIXED_RULE["maxDays"]:
                continue
            tol = max(5, med * 0.25)
            outliers = sum(1 for i in iv if abs(i - med) > tol)
            if outliers > (1 if len(iv) >= 4 else 0):
                continue
            last = ds[-1]
            active = (data_max - last).days <= med * FIXED_RULE["staleFactor"]
            found.append({
                "seller": seller, "amount": c["mean"], "periodDays": med,
                "periodLabel": _period_label(med), "n": len(c["rows"]),
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
    # amount 的非空保護留在這裡：只有月報在加總，其他讀取端不需要它
    invs = [v for v in db.invoices(conn) if v.amount is not None]

    items = _items_by_invoice(conn)
    # 單一 caller 的一次性查詢，照 db.py 的界線留在原地；翻譯在這裡做完，
    # 頁面拿到的就是中文（或未收錄的原始碼），不再解讀狀態碼
    status_raw = dict(conn.execute("select inv_num, inv_status from invoices"))
    months: dict[str, dict] = {}
    cats: dict[str, dict] = {}
    sellers: dict[str, dict] = {}
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
        # 非必要消費不另存一份清單：判準是 categories[].unnecessary 這個旗標，
        # 頁面用它篩 invoices 就好——以前兩種編碼並存，查詢頁用旗標、月報用
        # 清單，同一件事兩個答案
        st = status_raw.get(num)
        invoice_rows.append({"num": num, "date": str(inv_date)[:10],
                             "seller": si["display"], "category": cat.name,
                             "amount": amount, "items": inv_items,
                             "status": INVOICE_STATUS_ZH.get(st, st)})

    for mon in months.values():
        mon["byCategory"] = dict(mon["byCategory"])

    fda_rows = conn.execute("select count(*) from fda_rows").fetchone()[0]

    # 來源歸戶：match 報告的 source 欄是 source_url，反查 SOURCES 得名稱；
    # 直接寫名稱的（測試、未來格式）也照樣解析
    # 延後載入：fda 會拖 playwright
    from .sites.fda import SOURCES as _FDA_SOURCES, source_meta as _fda_source_meta
    url2name = {url: name for name, url in _FDA_SOURCES.items()}

    def src_info(src: str) -> tuple[str, str, str]:
        key = url2name.get(src, src or "")
        label, kind = _fda_source_meta(key)
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

    # 命中的層級直方圖不另外出一份：它從 matches 導得出來，而且兩種編碼會打架
    # ——沒有報告是 None、有報告零命中是 {}，後者在 JS 裡是 truthy，磚會從
    # 「—」翻成「0 筆」
    report = ws.match_report
    match_rows: list[dict] | None = None
    if report.exists():
        match_rows = []
        with report.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                s = src_bucket(row.get("source") or "")
                s["hits"] += 1
                match_rows.append({
                    "level": row.get("level"), "date": row.get("inv_date"),
                    "num": row.get("inv_num"),
                    "invoice": row.get("invoice_side"),
                    "fda": row.get("fda_side"), "source": s["key"],
                })

    # 對獎：號碼已在 lottery_draws（lottery 指令維護），這裡即算即得。
    # data.js 只放中獎結果與期別摘要，不放中獎號碼本身（UI 用不到）。
    from . import lottery as lottery_mod
    lot_raw = lottery_mod.check_invoices(conn, ws.cache)
    lottery = {
        "periods": [
            {"period": p["period"],
             "label": lottery_mod.period_label(p["period"]),
             "claimEnd": p["claim_end"],
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
        pending = len(db.invoices(conn, months=lottery_mod.period_months(np_)))
        lottery["next"] = {"label": lottery_mod.period_label(np_),
                          "drawDate": nd, "pending": pending}

    # 年度回顧（issue #13）：日曆年至今的全貌，統計與亮點都是「Python 決定
    # 的事實」，頁面只排版不重複聚合。年度＝庫內最新發票的年份，不用牆上
    # 時鐘——一月還沒抓新資料時不會出一頁空回顧，golden 也不吃當下日期。
    # 同額並列時取最早的（invoice_rows 依日期升冪，max 回傳第一個最大值）。
    # 這是第四份 rollup 迴圈（月度/分類/店家之後）——by_cat 雖可由 months
    # 導出，但 by_day 與 maxInvoice 只有列級資料算得出，一個迴圈掃完最直白。
    year = None
    if invoice_rows:
        yr = max(v["date"] for v in invoice_rows)[:4]
        yr_rows = [v for v in invoice_rows if v["date"].startswith(yr)]
        unn_cats = {c["name"] for c in cats.values() if c["unnecessary"]}
        by_cat: dict[str, float] = defaultdict(float)
        by_seller: dict[str, dict] = {}
        by_day: dict[str, dict] = {}
        unn = {"total": 0.0, "count": 0}
        for v in yr_rows:
            by_cat[v["category"]] += v["amount"]
            s = by_seller.setdefault(
                v["seller"], {"name": v["seller"], "total": 0.0, "count": 0})
            s["total"] += v["amount"]
            s["count"] += 1
            d = by_day.setdefault(
                v["date"], {"date": v["date"], "total": 0.0, "count": 0})
            d["total"] += v["amount"]
            d["count"] += 1
            if v["category"] in unn_cats:
                unn["total"] += v["amount"]
                unn["count"] += 1
        total = sum(v["amount"] for v in yr_rows)
        n_months = len({v["date"][:7] for v in yr_rows})
        top_inv = max(yr_rows, key=lambda v: v["amount"])
        wins = [w for p in lottery["periods"] for w in p["wins"]
                if w["date"].startswith(yr)]
        lot_inv = sum(
            p["nInvoices"] for p in lottery["periods"]
            if all(m.startswith(yr)
                   for m in lottery_mod.period_months(p["period"])))
        # monthCount 不叫 months——頂層 payload["months"] 是清單，同名異義
        year = {
            "year": yr, "total": total, "count": len(yr_rows),
            "monthCount": n_months, "monthlyAvg": total / n_months,
            "byCategory": sorted(
                ({"name": k, "total": t} for k, t in by_cat.items()),
                key=lambda c: -c["total"]),
            "sellers": sorted(by_seller.values(),
                              key=lambda s: -s["total"])[:10],
            "unnecessary": unn,
            "lottery": {"wins": len(wins),
                        "amount": sum(w["prizeAmount"] for w in wins),
                        "invoices": lot_inv},
            "maxInvoice": {k: top_inv[k]
                           for k in ("num", "date", "seller", "category",
                                     "amount")},
            "maxDay": max(by_day.values(), key=lambda d: d["total"]),
        }

    # 色槽：沿用上次指派（工作區本機狀態），新分類取空槽——跨次匯出
    # 同分類同色。serve 的重生也走這裡，兩條路徑不可能分岔。
    cat_rows = sorted(cats.values(), key=lambda c: -c["total"])
    slots = assign_slots(
        _load_slots(ws.state_path("catslots")),
        [c["name"] for c in cat_rows if c["name"] != UNCATEGORIZED])
    ws.ensure_state()
    ws.state_path("catslots").write_text(
        json.dumps(slots, ensure_ascii=False, indent=1), encoding="utf-8")
    for c in cat_rows:
        c["slot"] = slots.get(c["name"])

    # 「沒有任何規則命中」看 source，不是拿名字跟 UNCATEGORIZED 比
    uncategorized = sorted(
        (s for name, s in sellers.items()
         if (info.get(name) or {}).get("source", "none") == "none"),
        key=lambda s: -s["total"],
    )[:15]

    payload = {
        "generatedAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "invoiceCount": len(invs),
        "invoices": invoice_rows,
        "fixed": _detect_fixed(invoice_rows),
        "fixedRule": FIXED_RULE,
        "months": [months[k] for k in sorted(months)],
        "categories": cat_rows,
        "sellers": sorted(sellers.values(), key=lambda s: -s["total"]),
        "uncategorized": uncategorized,
        "fda": {"rows": fda_rows, "matches": match_rows,
                "sources": sorted(sources.values(),
                                  key=lambda s: (s["kind"] != "事件", s["label"]))},
        "lottery": lottery,
    }
    # 預算（issue #12）：沒設定就沒有這個鍵——「無磚」由鍵的有無決定，
    # 不用空物件（{} 在 JS 是 truthy，會讓磚以空狀態出現）。達成率由頁面
    # 以既有的月合計與非必要旗標組裝，這裡只出設定值。
    budget = load_budget(ws.budget)
    if budget:
        payload["budget"] = budget
    if year:
        payload["year"] = year
    return payload


def write_export(conn, ws: Workspace, classifier: Classifier,
                 web_dir: Path = WEB_DIR) -> Path:
    out_dir = ws.ensure_out()
    payload = build_payload(conn, ws, classifier)
    data_js = out_dir / "data.js"
    data_js.write_text(
        "window.TWCRAWL_DATA = "
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + ";\n",
        encoding="utf-8",
    )
    # 整個 web/ 一起複製，而不是逐檔明列：ui.css 是四頁的必要相依，漏掉一個
    # 檔案的後果從「少一頁」變成「四頁都壞但看起來像沒資料」。少列一個檔案
    # 不該是靜默的失敗模式
    for src in sorted(web_dir.rglob("*")):
        if src.is_dir():
            continue
        dest = out_dir / src.relative_to(web_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    dash = out_dir / "dashboard.html"
    n_unc = len(payload["uncategorized"])
    print(f"儀表板：{dash}（發票 {payload['invoiceCount']} 張、"
          f"{len(payload['months'])} 個月）")
    if n_unc:
        print(f"！有未分類店家（金額前 {n_unc} 名列在儀表板底部）"
              f"——想歸類就加進 categories.local.json")
    return dash
