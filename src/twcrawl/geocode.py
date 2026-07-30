"""twcrawl geocode — 把稅籍營業地址批次轉成座標（供地圖檢視）。

主力是內政部國土測繪中心（NLSC）TextQueryMap：免金鑰、台灣**門牌級**精度，
需帶 maps.nlsc.gov.tw 的 Referer。解不開時退 OSM Nominatim（台灣只有路段級，
標記會落在路中心）。查的都是**稅籍登記營業地址**——公開商工資料，不含個資。
結果快取回 `biz_registry.lat/lon`，只補缺漏、重跑安全；之後地圖全離線。

稅籍地址常見「○○里５鄰」與全形數字，兩個服務都可能噎到——先 NFKC 正規化、
截到「號」，再逐步降級（去里鄰 → 去門牌號取路段）。
"""

from __future__ import annotations

import json
import re
import ssl
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

NLSC = "https://api.nlsc.gov.tw/idc/TextQueryMap/"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")


def _ctx() -> ssl.SSLContext:
    # 政府憑證缺 SKI，關 Python 3.13 的嚴格檢查（鏈驗證與主機名照常）
    ctx = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def _clean(addr: str) -> str:
    a = unicodedata.normalize("NFKC", str(addr))
    a = re.sub(r"\s+", "", a)
    i = a.find("號")
    return a[: i + 1] if i != -1 else a


def _strip_village(a: str) -> str:
    # 里名不能貪婪跨過行政區界（例：測試區平安里5鄰 → 只刪「平安里5鄰」）
    a = re.sub(r"[^市區鄉鎮縣里村\d]{1,4}[里村]\d{1,3}鄰", "", a)
    return re.sub(r"\d{1,3}鄰", "", a)


def _road_level(a: str) -> str:
    return re.sub(r"[\d之-]+號$", "", a)


def _nlsc(address: str, timeout: int = 30) -> tuple[float, float] | None:
    req = urllib.request.Request(
        NLSC + urllib.parse.quote(address),
        headers={"User-Agent": _UA, "Referer": "https://maps.nlsc.gov.tw/"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    if not body.lstrip().startswith("<"):
        return None
    for item in ET.fromstring(body).iter("ITEM"):
        loc = (item.findtext("LOCATION") or "").strip()
        if "," in loc:
            lon, lat = loc.split(",")[:2]
            return float(lat), float(lon)
    return None


def _nominatim(address: str, timeout: int = 30) -> tuple[float, float] | None:
    url = NOMINATIM + "?" + urllib.parse.urlencode({
        "q": address, "format": "jsonv2", "limit": 1, "countrycodes": "tw"})
    req = urllib.request.Request(
        url, headers={"User-Agent": "twcrawl/0.1 (personal expense tool)"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
        hits = json.loads(resp.read().decode("utf-8"))
    return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None


def _resolve(addr: str, delay: float) -> tuple[float, float] | None:
    c = _clean(addr)
    ladder = [(_nlsc, c), (_nlsc, _strip_village(c)),
              (_nominatim, _strip_village(c)),
              (_nominatim, _road_level(_strip_village(c)))]
    seen: set[tuple] = set()
    for fn, q in ladder:
        if not q or (fn, q) in seen:
            continue
        seen.add((fn, q))
        try:
            pt = fn(q)
        except Exception:
            pt = None
        if pt:
            return pt
        time.sleep(delay)
    return None


def refresh(conn, delay: float = 1.0) -> int:
    rows = conn.execute(
        "select ban, name, address from biz_registry "
        "where address is not null and address != '' and lat is null"
    ).fetchall()
    if not rows:
        print("沒有待編碼的地址（全部已有座標）。")
        return 0
    print(f"待編碼 {len(rows)} 筆（NLSC 門牌定位為主、Nominatim 後備，限速 1 req/s）")
    ok = 0
    for i, (ban, name, addr) in enumerate(rows, 1):
        pt = _resolve(addr, delay)
        if pt:
            conn.execute(
                "update biz_registry set lat=?, lon=?, geocoded_at=datetime('now')"
                " where ban=?", (pt[0], pt[1], ban))
            ok += 1
        else:
            print(f"  [{i}/{len(rows)}] 解不出座標：{name}")
        if i % 10 == 0:
            conn.commit()
            print(f"  進度 {i}/{len(rows)}（成功 {ok}）")
        time.sleep(delay)
    conn.commit()
    print(f"完成：{ok}/{len(rows)} 筆取得座標（存回 biz_registry）")
    return ok
