"""讓 `python -m twcrawl` 可用。

控制台以子行程執行指令（issue #20），用 `sys.executable -m twcrawl` 就不必
去猜 console script 裝在哪裡——venv 的腳本目錄各平台不同（Windows 是
`Scripts\\twcrawl.exe`、POSIX 是 `bin/twcrawl`），而 `sys.executable` 一定
指向正在跑的直譯器，等於一定指向同一個環境。
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
