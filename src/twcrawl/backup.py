"""twcrawl backup／restore — AES-256 加密備份包的兩個方向。

隱私架構（docs/adr/0001）：本機為正、雲端只存密文。備份包收的東西列在
`_targets()`；`state/`（登入 cookie）永遠排除——等同帳號本體且數小時即失效，
沒有備份價值。

兩個方向放同一個模組，因為它們共用同一份「包裡有什麼、路徑長什麼樣」的知識：
收集範圍由 `_targets()` 出一份，還原就拿同一份推出可接受的包內路徑。分兩個
模組的話，備份多收一個目錄而還原不認得它，會在最需要它的那天才發現。
"""

from __future__ import annotations

import datetime as _dt
import shutil
from pathlib import Path, PurePosixPath

import pyzipper

from .workspace import Workspace

# 落地路徑越界的說法只有一份：`_entry_problem` 與 restore 的第二層防護會各講
# 一次，字面分兩份的話改一邊就漂。
OUT_OF_ROOT = "包內路徑會寫到工作區外"


def _targets(ws: Workspace) -> list[Path]:
    """備份收哪些東西——同時也是還原能接受哪些包內路徑的依據。

    個人設定（分類規則、預算）在 2026-08-02 的換機演練後補進來：它們是手工
    一條一條累積的、已 gitignore，而少了它們的還原是**靜默**降級——畫面照樣
    出得來，只是店家全部掉回通用規則、預算磚消失。`state/` 則永遠不收
    （ADR-0001），色槽那類本機狀態也不收（換機重指派即可）。

    不存在的路徑由 `_iter_files` 自然略過，所以沒設定預算的人包裡就沒有它。
    """
    return [ws.db, ws.captures, ws.rules, ws.budget]


def _iter_files(paths: list[Path]):
    for p in paths:
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from (f for f in sorted(p.rglob("*")) if f.is_file())


def _arcname(f: Path, root: Path) -> str:
    """壓縮包內的路徑一律相對工作區 root。

    以前是相對 `Path.cwd()`，所以同一份資料在不同目錄下備份會得到不同的包
    版面（還得為 Windows 磁碟機字母另開一條退路）。收進包的檔案一律在 root
    底下，相對化不會失敗。
    """
    return f.resolve().relative_to(root.resolve()).as_posix()


def make_backup(
    password: str,
    ws: Workspace,
    out_dir: Path | str | None = None,
) -> Path:
    if not password:
        raise ValueError("備份密碼不可為空")
    files = list(_iter_files(_targets(ws)))
    if not files:
        raise SystemExit(
            "沒有可備份的資料（資料庫、captures/ 與個人設定都不存在）。")
    for f in files:
        # 目錄名問 Workspace，不要在這裡再寫一份字面：改了那邊而這裡沒跟上，
        # 失敗的方式是登入 cookie 靜靜進了可上雲的包（ADR-0001）
        if ws.state.name in f.parts:
            raise AssertionError(f"備份絕不收 {ws.state.name}/：{f}")

    out_dir = Path(out_dir) if out_dir else ws.backup
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
            zf.write(f, arcname=_arcname(f, ws.root))

    size_mb = out_path.stat().st_size / 1_048_576
    print(f"備份包：{out_path}（{len(files)} 檔，{size_mb:.1f} MB，AES-256）")
    print("→ 這個檔案可以放 Google Drive；密碼請自行保管，遺失即不可讀。")
    return out_path


# ---- 還原 ----------------------------------------------------------------

def _entry_problem(name: str, prefixes: tuple[str, ...],
                   state_dir: str) -> str | None:
    """這個包內路徑不能還原的理由；None 代表可以。

    還原是「把一個外來的 zip 解進工作區」，所以包內路徑要當成不可信的輸入
    看：`..` 與絕對路徑會把檔案寫到工作區外，`state/` 則是 ADR-0001 的紅線。
    最後那條前綴比對同時也是「這是不是 twcrawl 備份包」的判準——備份收什麼、
    還原就只認什麼。

    `state_dir` 與 prefixes 都由呼叫端從 Workspace 推出，這個函式不自己記
    任何路徑字面。
    """
    if name.endswith("/"):
        return None  # 目錄項目沒有內容，目錄由檔案項目自己建
    p = PurePosixPath(name)
    if not p.parts or p.is_absolute() or ".." in p.parts:
        return OUT_OF_ROOT
    if state_dir in p.parts:
        return f"包內含 {state_dir}/（登入 cookie 永不備份，見 docs/adr/0001）"
    if not any(name == pre or name.startswith(pre + "/") for pre in prefixes):
        return "不是備份包會有的內容"
    return None


def _has_content(p: Path) -> bool:
    return p.is_file() or (p.is_dir() and any(f.is_file() for f in p.rglob("*")))


def existing_data(ws: Workspace) -> list[Path]:
    """工作區裡已經有、還原會蓋掉的東西。空清單代表可以直接還原。

    看的是「工作區有沒有資料」而不是「哪幾個檔會相撞」：把舊備份包還原到
    有新資料的工作區，就算檔名一個都沒撞上也是一場災難。

    範圍由 `_targets()` 導出而不是另外列一份。同一份清單手打第二次的話，
    備份日後多收一個東西時這裡會**靜默**漏掉它——而這條分支漏掉的後果，
    正好就是默默蓋掉使用者的資料。
    """
    return [p for p in _targets(ws) if _has_content(p)]


def restore_backup(archive: Path | str, password: str, ws: Workspace, *,
                   force: bool = False) -> dict:
    """把備份包解回工作區。

    順序是刻意的：**寫出第一個檔案之前，會失敗的理由全部先問完**——檔案在
    不在、工作區會不會被蓋掉、包的形狀對不對、密碼對不對。還原是資料毀損
    時才會跑的指令，解到一半才失敗比乾脆不動更糟。
    """
    archive = Path(archive)
    if not password:
        raise SystemExit("需要備份包密碼。")
    if archive.is_dir():
        raise SystemExit(
            f"{archive} 是目錄——請指定備份包檔案（twcrawl-backup-*.zip）。")
    if not archive.exists():
        raise SystemExit(
            f"找不到備份包：{archive}\n"
            "  → 確認路徑；備份包預設產在工作區的 out/backup/，"
            "檔名長得像 twcrawl-backup-20260802-0029.zip。")

    if not force:
        have = existing_data(ws)
        if have:
            # 印相對路徑：絕對路徑會把訊息撐到看不出重點，而目前目錄就在下一行
            listed = "、".join(p.relative_to(ws.root).as_posix() for p in have)
            raise SystemExit(
                f"這個工作區已經有資料，還原會覆蓋：{listed}\n"
                f"  目前目錄：{ws.root}\n"
                "  → 換到空目錄再還原（cd 過去再跑），"
                "或確定要覆蓋就加 --force。")

    prefixes = tuple(_arcname(p, ws.root) for p in _targets(ws))
    db_entry = _arcname(ws.db, ws.root)
    state_before = ws.state.exists()

    try:
        zf = pyzipper.AESZipFile(archive)
    except Exception as e:
        raise SystemExit(
            f"讀不到這個備份包（{archive}）：{e}\n"
            "  → 確認檔案完整、而且是 `twcrawl backup` 產生的 zip。") from None

    with zf:
        zf.setpassword(password.encode("utf-8"))
        names = zf.namelist()
        for name in names:
            why = _entry_problem(name, prefixes, ws.state.name)
            if why:
                raise SystemExit(
                    f"這不是 twcrawl 備份包——{why}：{name}\n"
                    "  → 只有 `twcrawl backup` 產生的包能還原；"
                    "手工壓的 zip 不行。")
        files = [n for n in names if not n.endswith("/")]
        if db_entry not in files:
            # 刻意不說「不是 twcrawl 備份包」——資料庫還不存在時 backup 也會
            # 產出一個只有個人設定的合法包，那句話會是假的
            raise SystemExit(
                f"這個備份包裡沒有資料庫（{db_entry}），還原了也沒有東西可用。\n"
                "  → 換一個備份包；有資料時產生的包一定含資料庫。")

        # 落地路徑也在前置階段算完並檢查。這道 resolve() 是 `_entry_problem`
        # 的第二層（字串比對萬一有洞，實際路徑不會騙人）；放在解壓迴圈裡的話
        # 它只能擋住那一個項目，前面幾個已經寫出去了。
        dests = []
        for name in files:
            dest = ws.root / name
            if ws.root.resolve() not in dest.resolve().parents:
                raise SystemExit(f"{OUT_OF_ROOT}：{name}")
            dests.append((name, dest))

        # 密碼在這裡就試，不要解到一半才發現。AES 的密碼驗證值在 open()
        # 時就檢查，所以讀不讀得到內容都一樣會擋下來。
        try:
            with zf.open(db_entry) as fh:
                fh.read(1)
        except RuntimeError as e:
            if "password" in str(e).lower():
                raise SystemExit(
                    "密碼錯誤，解不開這個備份包。\n"
                    "  → 就是產生這個包時輸入的那組密碼；"
                    "也可以設 TWCRAWL_BACKUP_PASSWORD 免互動輸入。") from None
            raise SystemExit(f"解不開這個備份包：{e}") from None

        total = 0
        for name, dest in dests:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            total += dest.stat().st_size

    # 紅線的第二道防線，對照 make_backup 的 assert：包裡不該有 state/
    # （上面逐項擋過），還原後也不該憑空多一個。
    if ws.state.exists() and not state_before:
        raise AssertionError(f"還原不該生出 {ws.state}")

    return {"archive": str(archive), "root": str(ws.root),
            "db": str(ws.db), "files": len(files), "bytes": total}
