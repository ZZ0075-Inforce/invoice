"""twcrawl bizreg — 財政部稅籍登記對照表（統編 → 行業、營業地址）。

「全國營業（稅籍）登記資料」（BGMOPEN1）是公開資料，約 66MB zip、每月更新。
下載後串流解析，**只保留資料庫裡出現過的統編**，其餘略過不落地；
結果進 `biz_registry` 表，export 用它做行業別自動分類與精確地圖連結。
"""

from __future__ import annotations

import csv
import io
import ssl
import urllib.request
import zipfile
from pathlib import Path

URL = "https://eip.fia.gov.tw/data/BGMOPEN1.zip"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    # 政府憑證缺 Subject Key Identifier，Python 3.13 預設的嚴格檢查會拒連；
    # 只關嚴格模式，憑證鏈與主機名驗證照常
    ctx = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    with urllib.request.urlopen(req, timeout=120, context=ctx) as resp, \
            open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and done % (10 << 20) < (1 << 20):
                print(f"  下載中 {done >> 20}/{total >> 20} MB")
    print(f"  已下載 {dest}（{dest.stat().st_size >> 20} MB）")


def _locate(header: list[str]) -> dict:
    """以欄名關鍵字定位欄位（同 match 的作法，官方改欄序不用改程式）。"""
    idx: dict = {"industry": []}
    for i, h in enumerate(header):
        h = h.strip()
        if h == "統一編號":
            idx["ban"] = i
        elif "營業人名稱" in h:
            idx["name"] = i
        elif "地址" in h:
            idx.setdefault("address", i)
        elif h.startswith("行業代號"):
            # 代號欄的下一欄是對應的「名稱」欄
            if i + 1 < len(header) and header[i + 1].strip().startswith("名稱"):
                idx["industry"].append((i, i + 1))
    missing = [k for k in ("ban", "name", "address") if k not in idx]
    if missing:
        raise SystemExit(f"BGMOPEN1 欄位定位失敗（{missing}），官方格式可能改版：{header[:8]}…")
    return idx


def refresh(conn, cache: Path | str, url: str = URL, force: bool = False) -> int:
    bans = {str(r[0]).strip() for r in conn.execute(
        "select distinct seller_ban from invoices where seller_ban is not null")}
    if not bans:
        raise SystemExit("資料庫裡沒有任何賣方統編，先跑 fetch。")

    cache = Path(cache)
    if force or not cache.exists():
        print(f"下載稅籍登記資料 {url}")
        _download(url, cache)
    else:
        print(f"沿用快取 {cache}（要更新加 --force）")

    n = 0
    with zipfile.ZipFile(cache) as z:
        member = next(m for m in z.namelist() if m.lower().endswith(".csv"))
        with z.open(member) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace")
            reader = csv.reader(text)
            idx = _locate(next(reader))
            for rec in reader:
                if len(rec) <= idx["ban"]:
                    continue
                ban = rec[idx["ban"]].strip()
                if ban not in bans:
                    continue
                pairs = [(rec[c].strip(), rec[m].strip())
                         for c, m in idx["industry"]
                         if len(rec) > m and rec[c].strip()]
                conn.execute(
                    "insert into biz_registry (ban, name, address, industry, industry_codes)"
                    " values (?,?,?,?,?)"
                    " on conflict(ban) do update set name=excluded.name,"
                    " address=excluded.address, industry=excluded.industry,"
                    " industry_codes=excluded.industry_codes,"
                    " fetched_at=datetime('now')",
                    (ban, rec[idx["name"]].strip(), rec[idx["address"]].strip(),
                     "、".join(nm for _c, nm in pairs if nm),
                     ",".join(c for c, _nm in pairs)),
                )
                n += 1
    conn.commit()
    print(f"對照表：命中 {n}/{len(bans)} 個統編（存入 biz_registry）")
    if n < len(bans):
        print("！未命中的多為已歇業或總機構報稅的統編，屬正常現象")
    return n
