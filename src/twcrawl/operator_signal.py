"""人工交接的訊號協定：終端機以外的前端怎麼說「我做完了」。

登入需要人工在瀏覽器操作，這件事沒有介面消得掉——平台有圖形驗證碼，專案
已定案不破（CLAUDE.md「已定案的決策」）。所以流程一定會停在某處等人。

等待的一端在**子行程**裡（`browser.wait_for_operator`），按下「我已登入」的
一端在 serve 進程（`jobs.Runner`、控制台頁），兩者之間只有環境變數與 stdout
可用，所以那個變數名與那行標記**必須是同一份定義**。各自手打一份的失敗方式
是靜默的：標記字串改了，控制台的按鈕從此不再出現，而終端機那條路（按 Enter）
照樣會動——不會有任何東西變紅。

這個模組不 import 其他 twcrawl 模組（比照 `capture_index`：跨界的格式由一個
沒有領域知識的模組定義，寫的一端與讀的一端都經過它）。
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

# 子行程改以「訊號檔出現」代替終端機輸入的開關；值就是那個檔案的路徑。
# 代理程式（Claude）背景啟動 login/capture 時本來就用這個，控制台沿用同一套。
ENV_DONE_FILE = "TWCRAWL_DONE_FILE"

# 「我正卡在人工交接」的機器可讀標記，由等待的一端印在 stdout。
# 同時印出的中文說明是給人看的、會改寫，前端不該拿它來判斷；這一行專門給程式對。
AWAITING_LINE = "[twcrawl:awaiting-operator]"


def done_file() -> Path | None:
    """這個行程要等的訊號檔；沒設就是走終端機（按 Enter）那條路。"""
    value = os.environ.get(ENV_DONE_FILE)
    return Path(value) if value else None


def is_awaiting(line: str) -> bool:
    """子行程的這一行是不是在說「我在等人工」。"""
    return AWAITING_LINE in line


def send(path: Path | str) -> None:
    """建立訊號檔＝「我做完了」。內容不重要，存在即是訊號。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("done", encoding="utf-8")


def taken(path: Path | str) -> bool:
    """訊號到了嗎？到了就順手收掉。

    收掉是必要的：同一個行程可能等第二次（capture 的多段操作），留著的話
    第二次會立刻被前一次的檔案放行，人根本還沒動手。
    """
    p = Path(path)
    if not p.exists():
        return False
    with contextlib.suppress(OSError):
        p.unlink()
    return True
