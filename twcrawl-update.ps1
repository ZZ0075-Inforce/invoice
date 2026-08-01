# twcrawl 每月例行的一鍵啟動器（issue #18）。
#
# 雙擊 twcrawl-update.bat 即可——不必先開終端機、不必 activate venv，
# 直接呼叫工作區 venv 裡的執行檔。建立桌面捷徑的方法見 README「一鍵啟動」。
#
# 為什麼主體在 .ps1 而不是 .bat：cmd.exe 逐位元組解析批次檔，用的是主控台的
# OEM codepage（繁中 Windows 是 cp950），UTF-8 的中文會被當成 Big5 讀而把
# 指令列切爛（實測跑出 'WL'、'安清單' 這類假指令，結束碼還錯給 0）。
# chcp 65001 救不了，因為壞在解析階段。PowerShell 讀 UTF-8 則是可靠的。

$ErrorActionPreference = 'Stop'

# 一律以「這兩個檔所在的目錄」為工作區——捷徑放桌面或任何地方都不影響
Set-Location -LiteralPath $PSScriptRoot

$twcrawl = Join-Path $PSScriptRoot '.venv\Scripts\twcrawl.exe'

function Wait-Then-Exit([int]$code) {
    Write-Host ''
    Write-Host '按 Enter 關閉這個視窗…' -ForegroundColor DarkGray
    # 從捷徑雙擊時沒有人接手輸出，直接結束就什麼都來不及看
    [void](Read-Host)
    exit $code
}

if (-not (Test-Path -LiteralPath $twcrawl)) {
    Write-Host ''
    Write-Host "找不到 $twcrawl" -ForegroundColor Yellow
    Write-Host ''
    Write-Host '這個啟動器必須放在 twcrawl 的工作區裡，而且該工作區要先建好虛擬環境：'
    Write-Host ''
    Write-Host '    python -m venv .venv'
    Write-Host '    .venv\Scripts\pip install -e .'
    Write-Host '    .venv\Scripts\python -m playwright install chromium'
    Write-Host ''
    Write-Host "目前所在目錄：$PSScriptRoot"
    Wait-Then-Exit 1
}

Write-Host '開始每月例行：登入 → 抓發票 → 食安清單 → 比對 → 對獎 → 報表 → 備份'
Write-Host ''
Write-Host '登入那一步會開瀏覽器，需要你本人操作（平台有圖形驗證碼，無法自動）；'
Write-Host '其餘全自動。跑完會自動開啟儀表板。'
Write-Host ''

& $twcrawl update
$rc = $LASTEXITCODE

if ($rc -ne 0) {
    # update 的設計是「一步失敗記錄後續跑、結尾彙總、回非零退出碼」，
    # 所以這裡不必猜是哪一步——上面的彙總已經寫了
    Write-Host ''
    Write-Host ('=' * 60) -ForegroundColor Yellow
    Write-Host "有步驟沒有跑完（結束碼 $rc）。" -ForegroundColor Yellow
    Write-Host '上面的彙總會指出是哪一步。一步失敗不影響其他步驟，'
    Write-Host '可以只重跑那一步，或直接再執行一次這個啟動器。'
    Write-Host ('=' * 60) -ForegroundColor Yellow
    Wait-Then-Exit $rc
}

# 成功就安靜收工：儀表板已經自動開了，留著主控台只是擋路
exit 0
