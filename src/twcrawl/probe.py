"""頁面結構偵察：把一頁的表格、表單、分頁控制項與 XHR 全部倒出來。

當某個站台的擷取結果不如預期時，先跑這個指令產生報告，
就能依實際 DOM 調整擷取規則，而不必逐次猜測。
"""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import Page

_INSPECT = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const q = (sel) => Array.from(document.querySelectorAll(sel));
  return {
    url: location.href,
    title: document.title,
    tables: q('table').map((t, i) => ({
      index: i, id: t.id || null, className: t.className || null,
      rows: t.rows.length,
      cols: t.rows[0] ? t.rows[0].cells.length : 0,
      firstRow: t.rows[0] ? Array.from(t.rows[0].cells).map(c => clean(c.innerText)) : [],
      sampleRow: t.rows[1] ? Array.from(t.rows[1].cells).map(c => clean(c.innerText)) : [],
    })),
    forms: q('form').map(f => ({
      id: f.id || null, action: f.action || null, method: f.method || null,
    })),
    selects: q('select').map(s => ({
      id: s.id || null, name: s.name || null,
      options: Array.from(s.options).slice(0, 20).map(o => clean(o.textContent)),
    })),
    buttons: q('input[type=submit], input[type=button], button').map(b => ({
      id: b.id || null, name: b.name || null,
      label: clean(b.value || b.innerText),
    })).slice(0, 60),
    postbackLinks: q('a[href^="javascript:__doPostBack"]').map(a => ({
      text: clean(a.innerText), href: a.getAttribute('href'),
    })).slice(0, 60),
    iframes: q('iframe').map(f => ({ id: f.id || null, src: f.src || null })),
  };
}
"""


def probe(page: Page, url: str, out_dir: Path = Path("out/probe")) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    xhr: list[dict] = []
    page.on(
        "response",
        lambda r: xhr.append({"url": r.url, "status": r.status,
                              "ct": (r.headers or {}).get("content-type", "")})
        if "json" in (r.headers or {}).get("content-type", "").lower()
        else None,
    )

    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass

    report = {"frames": [], "json_responses": [x for x in xhr if x]}
    for fr in page.frames:
        try:
            report["frames"].append(fr.evaluate(_INSPECT))
        except Exception as e:
            report["frames"].append({"url": fr.url, "error": str(e)})

    stem = "".join(c if c.isalnum() else "_" for c in url)[-60:]
    report_path = out_dir / f"probe_{stem}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"page_{stem}.html").write_text(page.content(), encoding="utf-8")

    print(f"偵察報告 → {report_path}")
    for f in report["frames"]:
        if "error" in f:
            continue
        print(f"\n[frame] {f['url']}")
        for t in f["tables"]:
            print(f"  table#{t['index']} id={t['id']} rows={t['rows']} cols={t['cols']}")
            if t["firstRow"]:
                print(f"      表頭: {t['firstRow']}")
        if f["selects"]:
            print(f"  selects: {[s['name'] or s['id'] for s in f['selects']]}")
        if f["postbackLinks"]:
            print(f"  分頁連結: {[p['text'] for p in f['postbackLinks']][:15]}")
    return report_path
