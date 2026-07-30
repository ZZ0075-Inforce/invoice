"""twcrawl backup — 產生可上雲的 AES-256 加密備份包。

隱私架構（docs/adr/0001）：本機為正、雲端只存密文。備份包收 SQLite 資料庫與
captures/ 原始擷取；`state/`（登入 cookie）永遠排除——等同帳號本體且數小時
即失效，沒有備份價值。
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pyzipper

from . import db

BACKUP_DIR = Path("out/backup")


def _iter_files(paths: list[Path]):
    for p in paths:
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from (f for f in sorted(p.rglob("*")) if f.is_file())


def _arcname(f: Path) -> str:
    try:
        return f.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        drive, tail = str(f.resolve()).replace("\\", "/").split(":", 1) \
            if ":" in str(f) else ("", f.as_posix())
        return tail.lstrip("/")


def make_backup(
    password: str,
    db_path: Path | str = db.DEFAULT_DB,
    captures_dir: Path | str = Path("captures"),
    out_dir: Path | str = BACKUP_DIR,
) -> Path:
    if not password:
        raise ValueError("備份密碼不可為空")
    targets = [Path(db_path), Path(captures_dir)]
    files = list(_iter_files(targets))
    if not files:
        raise SystemExit("沒有可備份的資料（資料庫與 captures/ 都不存在）。")
    for f in files:
        if "state" in f.parts:
            raise AssertionError(f"備份絕不收 state/：{f}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M")
    out_path = out_dir / f"twcrawl-backup-{stamp}.zip"

    with pyzipper.AESZipFile(
        out_path, "w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as zf:
        zf.setpassword(password.encode("utf-8"))
        for f in files:
            zf.write(f, arcname=_arcname(f))

    size_mb = out_path.stat().st_size / 1_048_576
    print(f"備份包：{out_path}（{len(files)} 檔，{size_mb:.1f} MB，AES-256）")
    print("→ 這個檔案可以放 Google Drive；密碼請自行保管，遺失即不可讀。")
    return out_path
