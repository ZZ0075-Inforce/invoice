"""產生可安全分享的擷取摘要。

只印出「有哪些端點」與「回應的欄位名稱與型別」，所有值（URL query、
POST 參數、JSON 內容、CSV 儲存格）一律代換成型別名稱，不含任何
消費紀錄或憑證，因此輸出可直接提供給接手的人對照欄位。
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from . import capture_index

INV_NUM_RE = re.compile(r"^[A-Z]{2}\d{8}$")
DATE_RE = re.compile(r"^\d{4}[/\-.]?\d{1,2}[/\-.]?\d{1,2}$")
ROC_DATE_RE = re.compile(r"^1\d{2}[/\-.]?\d{1,2}[/\-.]?\d{1,2}$")
CARRIER_RE = re.compile(r"^/[0-9A-Z+\-.]{7}$")
NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?$")


def _type_of(v: Any) -> str:
    """值 → 型別描述。字串附上格式提示（只描述格式，不洩漏內容）。"""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return "str(空)"
        if INV_NUM_RE.match(s.upper()):
            return "str(發票號碼格式)"
        if CARRIER_RE.match(s.upper()):
            return "str(手機條碼格式)"
        if ROC_DATE_RE.match(s):
            return "str(民國日期格式)"
        if DATE_RE.match(s):
            return "str(日期格式)"
        if NUM_RE.match(s):
            return "str(數值)"
        return f"str(len={len(s)})"
    return type(v).__name__


def _shape(node: Any) -> Any:
    """遞迴把 JSON 的值換成型別描述；dict 的 list 會合併各元素的欄位。"""
    if isinstance(node, dict):
        return {k: _shape(v) for k, v in node.items()}
    if isinstance(node, list):
        if not node:
            return "list(空)"
        if all(isinstance(x, dict) for x in node[:50]):
            merged: dict[str, Any] = {}
            for x in node[:50]:
                for k, v in x.items():
                    if k not in merged:
                        merged[k] = _shape(v)
            body: Any = merged
        else:
            body = _shape(node[0])
        return [body, f"…共 {len(node)} 筆"] if len(node) > 1 else [body]
    return _type_of(node)


def _safe_key(k: str) -> str:
    """參數「名稱」也可能是整個 JWT 或 token（電子發票平台就這麼做），一併遮蔽。"""
    if len(k) > 40 or re.match(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", k):
        return f"<token(len={len(k)})>"
    return k


def _safe_url(url: str) -> str:
    """去掉 query 的值，只留參數名稱與值的型別。"""
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else parts.path
    if not parts.query:
        return base
    qs = "&".join(
        f"{_safe_key(k)}=<{_type_of(v)}>"
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    )
    return f"{base}?{qs}"


def _safe_post(data: str) -> str:
    """POST body → 參數名稱與型別。JSON 與 form-encoded 皆可。"""
    s = data.strip()
    if s.startswith(("{", "[")):
        try:
            return json.dumps(_shape(json.loads(s)), ensure_ascii=False)
        except Exception:
            pass
    pairs = parse_qsl(s, keep_blank_values=True)
    if pairs:
        return "&".join(f"{_safe_key(k)}=<{_type_of(v)}>" for k, v in pairs)
    return f"(無法解析，{len(data)} 字元)"


def _cell_type(c: str) -> str:
    return _type_of(c)


def _maybe_csv(path: Path, indent: str = "    ") -> list[str]:
    """CSV／文字檔 → 每種列型的列數與欄位型別簽名（不輸出任何儲存格內容）。"""
    if not path.exists() or path.suffix.lower() not in (".csv", ".txt", ".bin"):
        return []
    from .sites.einvoice import _read_text

    try:
        rows = [r for r in csv.reader(io.StringIO(_read_text(path))) if r]
    except Exception:
        return [f"{indent}（無法以 CSV 解析）"]
    if not rows:
        return []

    # 首列若全為純文字（無數值、日期、發票號碼），視為表頭——
    # 欄位「名稱」不是個資，直接印出對校正最有幫助
    first = [c.strip() for c in rows[0]]
    if first and all(
        _type_of(c).startswith("str(len") or _type_of(c) == "str(空)" for c in first
    ):
        out = [f"{indent}表頭欄位: {', '.join(first)}"]
        if len(rows) > 1:
            sig = ", ".join(_cell_type(c) for c in rows[1])
            out.append(f"{indent}資料 {len(rows) - 1} 列，型別 [{sig}]")
        return out

    by_tag: dict[str, list[list[str]]] = {}
    for r in rows:
        tag = (r[0] or "").strip().upper()
        tag = tag if len(tag) <= 2 else "?"
        by_tag.setdefault(tag, []).append(r)
    out = []
    for tag, rs in sorted(by_tag.items()):
        sig = ", ".join(_cell_type(c) for c in rs[0])
        out.append(f"{indent}列型 {tag or '?'}：{len(rs)} 列，欄位型別 [{sig}]")
    return out


def summarize(root: Path) -> str:
    """讀 captures/<name>/，回傳可直接分享的純文字摘要。"""
    idx_path = capture_index.path(root)
    if not idx_path.exists():
        raise SystemExit(f"{idx_path} 不存在——這個目錄不是 capture 產生的結果。")
    entries = capture_index.read_entries(root)

    out = [
        f"擷取摘要：{root.name}",
        f"共 {len(entries)} 個項目。所有值已代換成型別名稱，可直接分享。",
    ]
    for e in entries:
        url = _safe_url(e.url)
        if e.kind == "download":
            out.append(f"\n[{e.seq:>03}] 下載  {url}")
            out.append(f"    {e.bytes:,} bytes")
            out += _maybe_csv(root / e.file)
            continue

        out.append(f"\n[{e.seq:>03}] {e.method or '?'} {e.status or '?'}  {url}")
        out.append(f"    content-type: {e.content_type or '?'}，{e.bytes:,} bytes")
        if e.post_data:
            out.append(f"    POST 參數: {_safe_post(e.post_data)}")
        f = root / e.file
        if f.suffix == ".json" and f.exists():
            try:
                shape = _shape(json.loads(f.read_text(encoding="utf-8", errors="replace")))
                pretty = json.dumps(shape, ensure_ascii=False, indent=2)
                out.append("    回應結構:")
                out.extend("      " + line for line in pretty.splitlines())
            except Exception:
                out.append("    （JSON 無法解析）")
        else:
            out += _maybe_csv(f)
    return "\n".join(out)
