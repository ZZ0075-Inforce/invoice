"""發票 × FDA 問題商品比對。

比對兩個層級：
- 店家層級：發票的賣方名稱 × 清單上的業者。「上榜通路」本身沒有行動價值
  （使用者實例：某超市 ×5 命中但從沒買過油品），所以有品項明細的發票會再
  與**該業者名下的下架產品**交叉檢查——全無交集就排除（計數照印）；
  只有無明細可查的發票保留純通路提示（標注）。
- 品項層級：發票品項 × 清單上的產品名稱（疑似直接買到問題商品）

FDA 各來源的欄位名稱不一致（業者/公司名稱/廠商、產品/品名…），
以關鍵字在每列 JSON 中自動定位，新增來源不需改比對邏輯。

品項／警訊層級只對「零售」有意義：餐飲現調店家（小吃店、快炒、手搖飲…）
的發票品項是菜名，撞包裝食品名必為誤報（例：菜名「蝦仁蛋炒飯」×
即食包「蝦仁蛋炒飯280g」）——就算店家真用了下架原料，菜名比對也
偵測不到，那要靠店家層級（不濾）。現調判定走兩路：店家分類（Classifier）
與稅籍行業（biz_registry × industry_category），其一屬餐飲類即濾，
濾除數與樣本照印保持透明。代價：餐飲店販售包裝品（咖啡店掛耳包）的
極端情境會漏，取精確度。
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from pathlib import Path

from .categories import Classifier, industry_category

_SELLER_KEYS = ("業者", "公司", "廠商", "商號", "責任廠商")
_PRODUCT_KEYS = ("產品", "品名", "品項", "商品")
_EATERY_CATS = {"餐飲", "速食", "手搖飲", "咖啡"}


def _norm(s) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))  # 全形→半形
    return re.sub(r"[\s　]+", "", s).lower()


def _fda_fields(rec: dict) -> tuple[str | None, str | None]:
    """從 FDA 列（欄名依來源而異）定位（業者, 產品）。"""
    seller = product = None
    for k, v in rec.items():
        nk = str(k)
        if seller is None and any(t in nk for t in _SELLER_KEYS):
            seller = v
        if product is None and any(t in nk for t in _PRODUCT_KEYS):
            product = v
    return seller, product


def run_match(conn, since: str | None = None, out_dir: Path = Path("out"),
              classifier: Classifier | None = None) -> dict[str, int]:
    cl = classifier or Classifier()
    industries = dict(conn.execute(
        "select ban, industry from biz_registry where industry is not null"))

    def _is_eatery(sname, ban) -> bool:
        if cl.category(sname) in _EATERY_CATS:
            return True
        return industry_category(industries.get(ban)) in _EATERY_CATS

    fda_rows = [
        (json.loads(data), src)
        for data, src in conn.execute("select data, source_url from fda_rows")
    ]
    sellers: dict[str, tuple[str, str]] = {}
    seller_products: dict[str, list[str]] = {}  # 業者 → 名下所有下架產品
    products: list[tuple[str, str, str, dict]] = []
    news: list[tuple[str, str, str]] = []  # 標題式來源（回收公告、警訊）
    for rec, src in fda_rows:
        s, p = _fda_fields(rec)
        ns, np_ = _norm(s), _norm(p)
        if len(ns) >= 3:
            sellers.setdefault(ns, (s, src))
        if len(np_) >= 3 and not np_.isdigit():
            products.append((np_, p, s or "", src))
            if len(ns) >= 3:
                seller_products.setdefault(ns, []).append(np_)
        if not s and not p:
            title = next((v for k, v in rec.items() if "標題" in str(k)), None)
            nt = _norm(title)
            if len(nt) >= 4:
                news.append((nt, str(title), src))

    cond, args = ("and inv_date >= ?", [since]) if since else ("", [])
    invs = conn.execute(
        f"select inv_num, inv_date, seller_name from invoices where 1=1 {cond}", args
    ).fetchall()
    items = conn.execute(
        "select i.inv_num, v.inv_date, v.seller_name, v.seller_ban, i.description "
        "from invoice_items i join invoices v on v.inv_num = i.inv_num "
        f"where i.description is not null {cond.replace('inv_date', 'v.inv_date')}",
        args,
    ).fetchall()

    print(f"FDA 清單 {len(fda_rows)} 筆（業者 {len(sellers)}、產品 {len(products)}、"
          f"警訊標題 {len(news)}）、發票 {len(invs)} 張、品項 {len(items)} 列"
          + (f"（{since} 起）" if since else ""))

    # 發票 → 正規化品項（店家命中時做品項排除用）
    items_by_inv: dict[str, list[str]] = {}
    for inv_num, _d, _s, _b, desc in items:
        nd = _norm(desc)
        if len(nd) >= 3 and not nd.isdigit():
            items_by_inv.setdefault(inv_num, []).append(nd)

    # 店家層級：「上榜通路」本身沒有行動價值（不能因為超市上過榜就不去）。
    # 有品項明細時交叉檢查——發票品項與該業者名下下架產品全無交集就排除；
    # 只有無明細可查的發票保留純通路提示。
    seller_hits: list[dict] = []
    seller_clears: list[dict] = []
    for inv_num, inv_date, sname in invs:
        n = _norm(sname)
        if len(n) < 3:
            continue
        for fn, (orig, src) in sellers.items():
            if fn in n or n in fn:
                hit = {
                    "level": "店家", "inv_num": inv_num, "inv_date": inv_date,
                    "invoice_side": sname, "fda_side": orig, "source": src,
                }
                inv_items = items_by_inv.get(inv_num)
                if inv_items:
                    prods = seller_products.get(fn, [])
                    if not any(nd in np_ or np_ in nd
                               for nd in inv_items for np_ in prods):
                        seller_clears.append(hit)
                        continue
                else:
                    hit["invoice_side"] = f"{sname}（無品項明細，僅通路提示）"
                seller_hits.append(hit)

    prod_hits: list[dict] = []
    news_hits: list[dict] = []
    eatery_skips: list[dict] = []
    for inv_num, inv_date, sname, ban, desc in items:
        nd = _norm(desc)
        if len(nd) < 3 or nd.isdigit():  # 純數字品項（價格/重量）必然誤報
            continue
        eatery = _is_eatery(sname, ban)
        for np_, orig_p, orig_s, src in products:
            if nd in np_ or np_ in nd:
                hit = {
                    "level": "品項", "inv_num": inv_num, "inv_date": inv_date,
                    "invoice_side": f"{desc}（{sname}）",
                    "fda_side": f"{orig_p}（{orig_s}）", "source": src,
                }
                (eatery_skips if eatery else prod_hits).append(hit)
        for nt, orig_t, src in news:
            if nd in nt:  # 品項名稱出現在回收/警訊標題中
                hit = {
                    "level": "警訊標題", "inv_num": inv_num, "inv_date": inv_date,
                    "invoice_side": f"{desc}（{sname}）",
                    "fda_side": orig_t, "source": src,
                }
                (eatery_skips if eatery else news_hits).append(hit)

    print(f"\n【店家 × 問題業者】{len(seller_hits)} 筆"
          "（發票品項與該業者下架產品有交集，或無明細可查）")
    for h in seller_hits:
        print(f"  {h['inv_date']} 發票 {h['inv_num']}「{h['invoice_side']}」"
              f"≈「{h['fda_side']}」")
    if seller_clears:
        print(f"\n【已排除】上榜通路的發票 {len(seller_clears)} 筆——"
              "品項皆不在該業者下架清單")
        by_seller: dict[str, int] = {}
        for h in seller_clears:
            by_seller[h["fda_side"]] = by_seller.get(h["fda_side"], 0) + 1
        for s, cnt in sorted(by_seller.items(), key=lambda x: -x[1]):
            print(f"  {s} ×{cnt}")
    print(f"\n【品項 × 問題產品】{len(prod_hits)} 筆")
    for h in prod_hits:
        print(f"  {h['inv_date']} 發票 {h['inv_num']} {h['invoice_side']}"
              f" ≈ {h['fda_side']}")
    print(f"\n【品項 × 回收/警訊標題】{len(news_hits)} 筆（字面命中，需人工確認）")
    for h in news_hits[:30]:
        print(f"  {h['inv_date']} 發票 {h['inv_num']} {h['invoice_side']}"
              f" ≈「{h['fda_side'][:60]}」")
    if len(news_hits) > 30:
        print(f"  …其餘 {len(news_hits) - 30} 筆見報告 CSV")
    if eatery_skips:
        print(f"\n【已濾除】餐飲現調店家的品項/標題撞名 {len(eatery_skips)} 筆"
              "（菜名撞包裝品名屬誤報；店家層級不受影響）")
        for h in eatery_skips[:5]:
            print(f"  {h['invoice_side']} ≈ {str(h['fda_side'])[:40]}")
        if len(eatery_skips) > 5:
            print(f"  …其餘 {len(eatery_skips) - 5} 筆")

    hits = seller_hits + prod_hits + news_hits
    if hits:
        out_dir.mkdir(parents=True, exist_ok=True)
        report = out_dir / "match_report.csv"
        with report.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(hits[0].keys()))
            w.writeheader()
            w.writerows(hits)
        print(f"\n報告 → {report}")
    else:
        print("\n→ 沒有任何匹配。")
    return {
        "seller_hits": len(seller_hits),
        "prod_hits": len(prod_hits),
        "news_hits": len(news_hits),
        "eatery_skips": len(eatery_skips),
        "seller_clears": len(seller_clears),
    }
