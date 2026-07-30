"""統一發票對獎：抓官方中獎號碼（本期＋上期）並比對庫內發票。

號碼來源是 invoice.etax.nat.gov.tw 的靜態頁——本期在首頁、上期在
lastNumber.html；再往前的期別已過領獎期，不抓。期別不能信頁面上的
期別字樣（導覽選單兩期都有），改由 tfoot「領獎期間」反推：
領獎起月 − 2 ＝ 期別末月（1-2 月期領獎起於次年 2 月，故需跨年處理）。

傳統獎項（特別獎～六獎、增開六獎）全部發票都參加，比末 8 碼。
雲端發票專屬獎（百萬／2,000／800／500 元）比**完整字軌＋8 碼**，
官方以 PDF 清冊公布（五百元獎百萬組、119MB）：cloudNowNumber／
cloudLastNumber 頁取「已排序」版 PDF、抽號碼存 gz 快取（一期一獎一檔，
PDF 抽完即刪不落地），之後比對只讀快取。同張同時符合傳統獎與雲端獎
依規定擇高。快取期別取自 PDF 檔名（20260506_…）而非頁面字樣。
"""

from __future__ import annotations

import gzip
import json
import re
import ssl
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

URLS = (
    "https://invoice.etax.nat.gov.tw/",              # 本期
    "https://invoice.etax.nat.gov.tw/lastNumber.html",  # 上期
)
CLOUD_PAGES = (
    "https://invoice.etax.nat.gov.tw/cloudNowNumber.html",
    "https://invoice.etax.nat.gov.tw/cloudLastNumber.html",
)
CLOUD_PRIZES = {  # PDF 檔名尾碼 → (獎名, 金額)
    "AI_C": ("雲端百萬元獎", 1_000_000),
    "AI_B": ("雲端兩千元獎", 2_000),
    "AI_E": ("雲端八百元獎", 800),
    "AI_D": ("雲端五百元獎", 500),
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 頭獎衍生獎次：(獎名, 比對末碼數, 金額)。特別獎/特獎為 8 碼全同，另列。
PRIZES = (
    ("頭獎", 8, 200_000),
    ("二獎", 7, 40_000),
    ("三獎", 6, 10_000),
    ("四獎", 5, 4_000),
    ("五獎", 4, 1_000),
    ("六獎", 3, 200),
)


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    ctx = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return resp.read().decode("utf-8", "replace")


def _strip_tags(fragment: str) -> str:
    # tag 換成空字串而非空格：頭獎號碼拆在相鄰 span（前 5＋後 3 碼、
    # 中間無文字節點），要靠這樣黏回；組與組之間有原始換行分隔。
    return re.sub(r"<[^>]+>", "", fragment)


def parse_draw(html: str) -> dict:
    """單頁 → {period, special, grand, first[], extra[], claim_start, claim_end}。

    號碼被拆在相鄰 span（前 5 碼＋後 3 碼），剝 tag 後自然黏回；只在
    「同期統一發票」規則說明之前的文字找數字，避免撞到獎金數字。
    結構不符（缺獎項、頭獎不是 3 組）就 raise——頁面改版要人來看，
    不能默默錯對。
    """
    m = re.search(r"領獎期間自(\d{3})年(\d{2})月(\d{2})日起至"
                  r"(\d{3})年(\d{2})月(\d{2})日止", html)
    if not m:
        raise ValueError("找不到「領獎期間」，頁面結構可能已改版")
    roc_y, claim_m = int(m[1]), int(m[2])
    end_month, period_year = claim_m - 2, roc_y
    if end_month <= 0:
        end_month, period_year = end_month + 12, period_year - 1
    draw = {
        "period": f"{period_year}{end_month - 1:02d}",
        "special": None, "grand": None, "first": [], "extra": [],
        "claim_start": f"{int(m[1]) + 1911:04d}-{m[2]}-{m[3]}",
        "claim_end": f"{int(m[4]) + 1911:04d}-{m[5]}-{m[6]}",
    }
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        txt = _strip_tags(tr).strip()
        nums_part = txt.split("同期統一發票")[0]
        if txt.startswith("特別獎"):
            got = re.findall(r"(?<!\d)(\d{8})(?!\d)", nums_part)
            draw["special"] = got[0] if got else draw["special"]
        elif txt.startswith("特獎"):
            got = re.findall(r"(?<!\d)(\d{8})(?!\d)", nums_part)
            draw["grand"] = got[0] if got else draw["grand"]
        elif txt.startswith("頭獎"):
            got = re.findall(r"(?<!\d)(\d{8})(?!\d)", nums_part)
            draw["first"] = got or draw["first"]
        elif txt.startswith("增開六獎"):
            got = re.findall(r"(?<!\d)(\d{3})(?!\d)", nums_part)
            draw["extra"] = got or draw["extra"]
    if not (draw["special"] and draw["grand"] and len(draw["first"]) == 3):
        raise ValueError(f"中獎號碼解析不完整：{draw}，頁面結構可能已改版")
    return draw


def match_number(num8: str, draw: dict) -> tuple[str, int] | None:
    """發票末 8 碼對一期號碼 → (獎名, 金額)；沒中回 None。

    多組頭獎各自比對，取最高獎級；增開六獎只在沒有更高獎時算。
    """
    if num8 == draw["special"]:
        return ("特別獎", 10_000_000)
    if num8 == draw["grand"]:
        return ("特獎", 2_000_000)
    best = None
    for win in draw["first"]:
        for name, tail, amount in PRIZES:
            if num8[-tail:] == win[-tail:]:
                if best is None or amount > best[1]:
                    best = (name, amount)
                break
    if best is None:
        for extra in draw["extra"]:
            if num8[-3:] == extra:
                best = ("增開六獎", 200)
                break
    return best


def period_months(period: str) -> list[str]:
    """期別 "11505" → 發票月份 ["2026-05", "2026-06"]。"""
    y, m = int(period[:3]) + 1911, int(period[3:])
    return [f"{y:04d}-{m:02d}", f"{y:04d}-{m + 1:02d}"]


def period_label(period: str) -> str:
    return f"{period[:3]}年{period[3:]}-{int(period[3:]) + 1:02d}月"


def next_draw(period: str) -> tuple[str, str]:
    """最新期別 → (下期 period, 下期開獎日 ISO)。開獎固定在期別末月
    次月的 25 日（5-6 月期 7/25 開 → 下期 7-8 月期 9/25 開）。"""
    y, m = int(period[:3]), int(period[3:])
    nm, ny = m + 2, y
    if nm > 12:
        nm, ny = nm - 12, ny + 1
    dm, dy = nm + 2, ny
    if dm > 12:
        dm, dy = dm - 12, dy + 1
    return f"{ny}{nm:02d}", f"{dy + 1911:04d}-{dm:02d}-25"


def _download_pdf_numbers(url: str, dest: Path) -> int:
    """下載清冊 PDF、抽出全部號碼寫入 gz（一行一號）；PDF 用完即刪。"""
    import tempfile

    from pypdf import PdfReader
    from pypdf import filters as _pdf_filters

    # 五百元獎清冊的單一內容 stream 解壓後約 80MB，會踩 pypdf 預設 75MB
    # 的解壓炸彈防護（LimitReachedError）；來源是官方固定 URL，放寬到
    # 512MB 而非關閉（=0），保留護欄
    _pdf_filters.ZLIB_MAX_OUTPUT_LENGTH = 512_000_000
    _pdf_filters.MAX_DECLARED_STREAM_LENGTH = 512_000_000

    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    ctx = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    n = 0
    with tempfile.TemporaryDirectory() as td:
        tmp_pdf = Path(td) / "list.pdf"
        with urllib.request.urlopen(req, timeout=600, context=ctx) as resp, \
                open(tmp_pdf, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            big = total > (20 << 20)
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if big:
                    print(f"\r  下載 {done >> 20}/{total >> 20} MB",
                          end="", flush=True)
            if big:
                print()
        # Windows：reader 吃外部檔控制代碼，with 結束先關檔才能清暫存目錄
        with open(tmp_pdf, "rb") as fh:
            reader = PdfReader(fh)
            pages = len(reader.pages)
            with gzip.open(dest, "wt", encoding="ascii") as out:
                for i, pg in enumerate(reader.pages):
                    for num in re.findall(r"[A-Z]{2}\d{8}",
                                          pg.extract_text() or ""):
                        out.write(num + "\n")
                        n += 1
                    if pages > 500 and (i + 1) % 500 == 0:
                        print(f"\r  解析 {i + 1}/{pages} 頁（{n:,} 組）",
                              end="", flush=True)
            if pages > 500:
                print()
    return n


def refresh_cloud(cache_dir: Path) -> list[str]:
    """抓雲端專屬獎清冊入快取；回傳本次新建的快取檔名。

    只取「已排序」版 PDF（小 2 成）；同期同獎已有快取就跳過——官方
    同一期檔名固定（含產檔時間戳），不會原地改版。
    """
    got = []
    for page_url in CLOUD_PAGES:
        try:
            html = _fetch(page_url)
        except Exception as e:  # noqa: BLE001 — 單頁失敗不擋另一頁
            print(f"！抓取雲端獎頁失敗（{page_url}）：{e}")
            continue
        for href in re.findall(r'href="([^"]*sorted_AI_[A-Z]\.pdf)"', html):
            fname = href.rsplit("/", 1)[-1]
            m = re.match(r"(\d{4})(\d{2})\d{2}_.*_(AI_[A-Z])\.pdf$", fname)
            if not m or m[3] not in CLOUD_PRIZES:
                continue
            period, tag = f"{int(m[1]) - 1911}{m[2]}", m[3]
            dest = cache_dir / f"cloud_{period}_{tag}.txt.gz"
            if dest.exists():
                continue
            print(f"下載雲端獎清冊：{CLOUD_PRIZES[tag][0]}"
                  f"（{period_label(period)}）…")
            try:
                n = _download_pdf_numbers(
                    urllib.parse.urljoin(page_url, href), dest)
            except Exception as e:  # noqa: BLE001
                print(f"！失敗：{e}")
                dest.unlink(missing_ok=True)   # 半成品不留（會擋重抓）
                continue
            print(f"  ✓ {dest.name}：{n:,} 組號碼入快取")
            got.append(dest.name)
    return got


def _cloud_hits(period: str, full_nums: set[str],
                cache_dir: Path) -> dict[str, tuple[str, int]]:
    """該期雲端獎比對：完整字軌號碼 ∈ 清冊。回 {完整號碼: (獎名, 金額)}。"""
    hits: dict[str, tuple[str, int]] = {}
    if not full_nums:
        return hits
    for tag, (pname, amt) in CLOUD_PRIZES.items():
        p = cache_dir / f"cloud_{period}_{tag}.txt.gz"
        if not p.exists():
            continue
        with gzip.open(p, "rt", encoding="ascii") as f:
            for line in f:
                num = line.strip()
                if num in full_nums and (num not in hits or amt > hits[num][1]):
                    hits[num] = (pname, amt)
    return hits


def refresh_draws(conn: sqlite3.Connection) -> list[str]:
    """抓兩頁號碼入庫；回傳更新到的期別。網路失敗只警告（庫存還能對）。"""
    from . import db

    got = []
    for url in URLS:
        try:
            draw = parse_draw(_fetch(url))
        except Exception as e:  # noqa: BLE001 — 單頁失敗不該擋另一頁
            print(f"！抓取中獎號碼失敗（{url}）：{e}")
            continue
        db.upsert_lottery_draws(conn, [draw])
        got.append(draw["period"])
    return got


def check_invoices(conn: sqlite3.Connection, cache_dir: Path) -> dict:
    """庫內發票 × 庫內號碼逐期對獎（純讀取；結果即算即得，不落地）。

    傳統獎比末 8 碼；雲端專屬獎比完整字軌號碼（讀 gz 快取，沒快取就
    只對傳統獎）。同張兩類都中依規定擇高，較低者記在 also。
    """
    draws = [
        {
            "period": r["period"], "special": r["special"], "grand": r["grand"],
            "first": json.loads(r["first_json"] or "[]"),
            "extra": json.loads(r["extra_json"] or "[]"),
            "claim_start": r["claim_start"], "claim_end": r["claim_end"],
        }
        for r in conn.execute(
            "select * from lottery_draws order by period desc")
    ]
    covered: set[str] = set()
    periods = []
    for d in draws:
        months = period_months(d["period"])
        covered.update(months)
        rows = conn.execute(
            "select inv_num, inv_date, seller_name, amount from invoices "
            "where substr(inv_date, 1, 7) in (?, ?) order by inv_date",
            months).fetchall()
        cloud = _cloud_hits(d["period"],
                            {str(r["inv_num"]) for r in rows}, cache_dir)
        wins = []
        for r in rows:
            full = str(r["inv_num"])
            num8 = full[-8:]
            trad = match_number(num8, d) if num8.isdigit() else None
            cl = cloud.get(full)
            if not trad and not cl:
                continue
            hi, lo = ((trad, cl) if (trad and (not cl or trad[1] >= cl[1]))
                      else (cl, trad))
            wins.append({
                "inv_num": full, "date": r["inv_date"],
                "seller": r["seller_name"], "amount": r["amount"],
                "prize": hi[0], "prize_amount": hi[1],
                "also": lo[0] if lo else None,   # 擇高後另一個符合的獎
            })
        periods.append({**d, "months": months, "n_invoices": len(rows),
                        "cloud_checked": sorted(
                            tag for tag in CLOUD_PRIZES
                            if (cache_dir / f"cloud_{d['period']}_{tag}.txt.gz"
                                ).exists()),
                        "wins": wins})
    marks = ",".join("?" * len(covered))
    if marks:
        n_out = conn.execute(
            "select count(*) from invoices where substr(inv_date, 1, 7) "
            f"not in ({marks})", sorted(covered)).fetchone()[0]
    else:
        n_out = conn.execute("select count(*) from invoices").fetchone()[0]
    return {"periods": periods, "uncovered": n_out}


def run_lottery(conn: sqlite3.Connection, cache_dir: Path,
                fetch: bool = True, cloud: bool = True) -> dict:
    print("=== 統一發票對獎 ===")
    if fetch:
        got = refresh_draws(conn)
        if got:
            print("已更新中獎號碼：" + "、".join(period_label(p) for p in got))
        if cloud:
            refresh_cloud(cache_dir)
    result = check_invoices(conn, cache_dir)
    if not result["periods"]:
        print("資料庫還沒有任何中獎號碼（網路抓取失敗？），無從對起。")
        return result
    total_wins = total_amount = 0
    for p in result["periods"]:
        n_cloud = len(p["cloud_checked"])
        cloud_note = (f"；雲端獎清冊 {n_cloud}/4" if n_cloud < 4
                      else "；含雲端專屬獎")
        print(f"\n--- {period_label(p['period'])}（{p['n_invoices']} 張參加；"
              f"領獎 {p['claim_start']} ～ {p['claim_end']}{cloud_note}）---")
        for w in p["wins"]:
            also = f"（同張亦符合{w['also']}，依規定擇高）" if w["also"] else ""
            print(f"  🎉 {w['prize']} NT${w['prize_amount']:,}  {w['inv_num']}"
                  f"  {w['date']}  {w['seller'] or '(店家不明)'}{also}")
        if p["wins"]:
            amt = sum(w["prize_amount"] for w in p["wins"])
            print(f"  共中 {len(p['wins'])} 筆、NT${amt:,}")
            total_wins += len(p["wins"])
            total_amount += amt
        else:
            print("  沒有中獎")
    n = sum(p["n_invoices"] for p in result["periods"])
    print(f"\n共 {len(result['periods'])} 期 {n} 張參加、"
          f"中 {total_wins} 筆、合計 NT${total_amount:,}")
    if result["uncovered"]:
        print(f"（另有 {result['uncovered']} 張發票不在已開獎期別內：",
              "尚未開獎或已過領獎期）", sep="")
    return result
