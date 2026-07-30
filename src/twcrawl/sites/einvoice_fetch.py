"""登入後的逐月自動抓取（重放平台 API，不點 UI）。

平台 UI 一次只能查一個月，但 API 沒有這個限制以外的門檻，因此以月為單位
逐月呼叫即可涵蓋任意區間：

    getSearchCarrierInvoiceListJWT  {dates…}      → 查詢用 token
    searchCarrierInvoice?page=N     {"token": …}  → 該月發票（Spring Data 分頁）
    common/getCarrierInvoiceDetail  <每列自帶的 token>  → 該張發票的品項明細

呼叫一律透過已登入頁面自己的 fetch() 發出，Cookie／CORS 與 SPA 完全一致。
認證用的 bearer token 由 SPA 開機時放進 sessionStorage（**不在 cookie 裡**，
storage_state 也不會保存它），因此每次都從頁面即時取用，不落地儲存。

回應原樣寫入 captures/，再交給既有的 ingest 解析，因此解析規則日後調整
可直接 `twcrawl ingest` 重跑，不必重新抓取。
"""

from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import BrowserContext

from .. import db
from . import einvoice

API = "https://service-mc.einvoice.nat.gov.tw/btc/cloud/api"
JWT_URL = f"{API}/btc502w/getSearchCarrierInvoiceListJWT"
SEARCH_URL = f"{API}/btc502w/searchCarrierInvoice"
DETAIL_URL = f"{API}/common/getCarrierInvoiceDetail"

TPE = timezone(timedelta(hours=8))
PAGE_SIZE = 50

# 在頁面內發請求。認證 token 在瀏覽器端就地取用，不回傳到 Python 這邊。
_FETCH = """
async ({url, body, isJson}) => {
  const t = sessionStorage.getItem('token');
  if (!t) return {status: -1, text: ''};
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': isJson ? 'application/json' : 'text/plain',
      'Authorization': 'Bearer ' + t,
    },
    body: body,
  });
  return {status: res.status, text: await res.text()};
}
"""

_TOKEN_READY = "() => !!sessionStorage.getItem('token')"


def _months(start: str, end: str) -> list[tuple[int, int]]:
    """'2026-03'～'2026-06' → [(2026,3), …, (2026,6)]。"""
    try:
        y1, m1 = (int(x) for x in start.split("-")[:2])
        y2, m2 = (int(x) for x in end.split("-")[:2])
    except ValueError:
        raise SystemExit("月份格式應為 YYYY-MM，例如 --from 2026-03 --to 2026-06")
    if (y1, m1) > (y2, m2):
        raise SystemExit("起始月份不可晚於結束月份。")
    out = []
    y, m = y1, m1
    while (y, m) <= (y2, m2):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _utc_range(year: int, month: int, now: datetime | None = None) -> tuple[str, str]:
    """該月的台北時間起訖 → 平台使用的 UTC ISO 字串（24 字元）。

    結束時間夾到「現在」為止：平台對未來日期會回 HTTP 400，
    因此當月查詢不能直接送月底。
    """
    last = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=TPE)
    end = datetime(year, month, last, 23, 59, 59, tzinfo=TPE)
    now = now or datetime.now(TPE)
    if end > now:
        end = now

    def iso(dt: datetime) -> str:
        u = dt.astimezone(timezone.utc)
        return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"

    return iso(start), iso(end)


def _post(page, url: str, body: str, is_json: bool = True) -> tuple[int, str]:
    res = page.evaluate(_FETCH, {"url": url, "body": body, "isJson": is_json})
    return res["status"], res["text"]


class _Sink:
    """把回應存成與 capture 相同的目錄結構，好讓既有的 ingest 直接吃。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "responses").mkdir(parents=True, exist_ok=True)
        self.index: list[dict] = []
        self.seq = 0

    def add(self, name: str, text: str, post: str = "") -> None:
        self.seq += 1
        rel = f"responses/{self.seq:03d}_{name}.json"
        (self.root / rel).write_text(text, encoding="utf-8")
        entry = {
            "seq": self.seq, "kind": "response", "method": "POST", "status": 200,
            "url": name, "content_type": "application/json",
            "bytes": len(text.encode()), "file": rel,
        }
        if post:
            entry["request_post_data"] = post
        self.index.append(entry)
        (self.root / "index.json").write_text(
            json.dumps(self.index, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class _AuthError(Exception):
    """登入失效——與「單月抓取失敗」不同，必須中止整批。"""


def _fetch_month(
    page, sink: "_Sink", year: int, month: int, with_details: bool, now: datetime
) -> tuple[int, int]:
    s, e = _utc_range(year, month, now)
    body = json.dumps({
        "cardCode": "", "carrierId2": "",
        "searchStartDate": s, "searchEndDate": e,
        "invoiceStatus": "all", "isSearchAll": "true",
    })
    status, token = _post(page, JWT_URL, body)
    token = token.strip().strip('"')
    if status in (-1, 401):
        raise _AuthError("登入已失效，請重跑 `twcrawl login`。")
    if status != 200 or not token:
        raise RuntimeError(f"取得查詢 token 失敗，HTTP {status}")

    tokens: list[str] = []
    page_no, pages = 0, 1
    while page_no < pages:
        url = f"{SEARCH_URL}?page={page_no}&size={PAGE_SIZE}"
        status, text = _post(page, url, json.dumps({"token": token}))
        if status in (-1, 401):
            raise _AuthError("登入已失效，請重跑 `twcrawl login`。")
        if status != 200:
            raise RuntimeError(f"第 {page_no + 1} 頁查詢失敗，HTTP {status}")
        sink.add(f"searchCarrierInvoice_{year}{month:02d}_p{page_no}", text)
        try:
            data = json.loads(text)
        except ValueError:
            raise RuntimeError("回應不是 JSON，格式可能已變更")
        pages = max(1, int(data.get("totalPages") or 1))
        rows = data.get("content") or []
        tokens += [r["token"] for r in rows if isinstance(r, dict) and r.get("token")]
        page_no += 1

    print(f"  {year}-{month:02d}：發票 {len(tokens)} 張", end="", flush=True)

    n_detail = 0
    if with_details:
        for t in tokens:
            status, text = _post(page, DETAIL_URL, t, is_json=False)
            if status != 200:
                continue  # 個別明細失敗不中斷該月
            sink.add(f"getCarrierInvoiceDetail_{year}{month:02d}", text, post=t)
            n_detail += 1
        print(f"、明細 {n_detail} 份", end="")
    print(flush=True)
    return len(tokens), n_detail


def fetch_range(
    ctx: BrowserContext,
    conn,
    start: str,
    end: str,
    with_details: bool = True,
) -> dict[str, int]:
    months = _months(start, end)
    page = ctx.new_page()
    page.goto(einvoice.HOME, wait_until="domcontentloaded")
    try:
        page.wait_for_function(_TOKEN_READY, timeout=30_000)
    except Exception:
        raise SystemExit(
            "頁面未取得認證 token（sessionStorage.token）。"
            "登入狀態可能已失效，請重跑 `twcrawl login`。"
        )

    stamp = datetime.now(TPE).strftime("%Y%m%d-%H%M%S")
    sink = _Sink(Path("captures") / f"einvoice-fetch-{stamp}")

    now = datetime.now(TPE)
    total_inv = 0
    total_detail = 0
    failed: list[str] = []
    for year, month in months:
        label = f"{year}-{month:02d}"
        if (year, month) > (now.year, now.month):
            print(f"  {label}：尚未到來，略過")
            continue
        try:
            n_month, n_detail = _fetch_month(
                page, sink, year, month, with_details=with_details, now=now
            )
        except _AuthError as exc:
            raise SystemExit(f"{label} {exc}") from None
        except Exception as exc:  # 單月失敗不該讓整批作廢
            failed.append(label)
            print(f"  {label}：失敗（{exc}），繼續下一個月")
            continue
        total_inv += n_month
        total_detail += n_detail

    if failed:
        print(f"\n⚠️ 下列月份未取得：{', '.join(failed)}（其餘已抓回，可單獨重跑該月）")
    print(f"\n抓取完成 → {sink.root}")
    res = einvoice.ingest(sink.root, conn)
    return {"invoices": total_inv, "details": total_detail, **res}
