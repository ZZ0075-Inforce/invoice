"""twcrawl workspace — 工作區：所有本機路徑的單一來源。

工作區是一個目錄，底下收著資料庫、擷取結果、登入狀態與個人規則；工具的所有
路徑由它推出（見 CONTEXT.md「工作區」）。指令一律在工作區內執行——CLI 傳
`Path.cwd()`，測試傳暫存目錄。root 是參數而不是模組常數，正是因為這兩個
adapter 都真實存在；在此之前測試只能用 `os.chdir` 冒充第二個。

這個模組不 import 其他 twcrawl 模組，方向永遠是「別人依賴工作區」。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

TPE = _dt.timezone(_dt.timedelta(hours=8))


@dataclass(frozen=True)
class Workspace:
    """一個工作區的佈局。所有路徑唯讀衍生，建立目錄的動作明確標在方法名上。"""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    # -- 路徑 ----------------------------------------------------------------
    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def db(self) -> Path:
        return self.out / "twcrawl.sqlite"

    @property
    def captures(self) -> Path:
        return self.root / "captures"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def cache(self) -> Path:
        return self.out / "cache"

    @property
    def rules(self) -> Path:
        return self.root / "categories.local.json"

    @property
    def budget(self) -> Path:
        return self.root / "budget.local.json"

    @property
    def backup(self) -> Path:
        return self.out / "backup"

    @property
    def match_report(self) -> Path:
        return self.out / "match_report.csv"

    @property
    def bizreg_cache(self) -> Path:
        return self.out / "bgmopen1.zip"

    @property
    def probe_out(self) -> Path:
        return self.out / "probe"

    def state_path(self, name: str) -> Path:
        return self.state / f"{name}.json"

    def handoff_path(self, capture_name: str) -> Path:
        return self.out / f"handoff_{capture_name}.txt"

    # -- 佈局知識 ------------------------------------------------------------
    def ensure_out(self) -> Path:
        self.out.mkdir(parents=True, exist_ok=True)
        return self.out

    def ensure_state(self) -> Path:
        self.state.mkdir(parents=True, exist_ok=True)
        return self.state

    def new_capture(self, name: str) -> Path:
        """開一個新的擷取目錄（含 responses/ 與 downloads/）並回傳。

        時間戳一律用台北時間，與發票日期同一個時區基準，不隨機器時區漂移。
        擷取與 fetch 兩條路徑共用這裡，目錄形狀才只有一份定義。
        """
        stamp = _dt.datetime.now(TPE).strftime("%Y%m%d-%H%M%S")
        root = self.captures / f"{name}-{stamp}"
        (root / "responses").mkdir(parents=True, exist_ok=True)
        (root / "downloads").mkdir(parents=True, exist_ok=True)
        return root

    def latest_capture(self, prefix: str = "einvoice") -> Path:
        """最近一次擷取目錄——依 mtime，不依檔名字典序。

        字典序會讓 `einvoice-fetch-*` 恆勝 `einvoice-2*`（"f" > "2"），於是
        `ingest`／`handoff` 不加 --dir 就永遠選到最新的 fetch，選不到更新的
        人工 capture。
        """
        roots = [p for p in self.captures.glob(f"{prefix}-*") if p.is_dir()]
        if not roots:
            raise SystemExit(
                f"找不到任何擷取結果（{self.captures}）。\n"
                "  → 先執行 `twcrawl capture` 或 `twcrawl fetch`。"
            )
        return max(roots, key=lambda p: p.stat().st_mtime)

    def require_db(self) -> Path:
        """讀取型指令的前置條件。

        `db.connect` 會替不存在的檔案建一個空資料庫，所以在錯的目錄跑
        `export` 只會默默產出一份空儀表板。讀取型指令先問過這裡，才不會把
        「跑錯目錄」看成「你沒有資料」。
        """
        if not self.db.exists():
            raise SystemExit(
                f"這裡不是 twcrawl 工作區——找不到 {self.db}\n"
                f"  目前目錄：{self.root}\n"
                "  → 先 cd 到你的工作區，或先跑 `twcrawl fetch` 建立資料。"
            )
        return self.db
