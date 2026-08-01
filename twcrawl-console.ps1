# twcrawl 一鍵啟動器（issue #18；#21 起改為開控制台）。
#
# 雙擊 twcrawl-console.bat 即可——不必先開終端機、不必 activate venv，
# 直接呼叫工作區 venv 裡的執行檔。建立桌面捷徑的方法見 README「一鍵啟動」。
#
# 這個視窗跑的是本機小站（serve）本身，不是一次性的工作：每月例行、抓發票、
# 重生報表全都從它開出來的控制台頁按。關掉視窗就整個停止。
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
    # 從捷徑雙擊時沒有人接手輸出，直接結束就什麼都來不及看。
    # 非互動地被呼叫時（測試、管線）讀不到輸入，不要因此炸掉結束碼
    try { [void](Read-Host) } catch { }
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

# 備份密碼在這裡問一次。控制台啟動的工作是子行程，而且 stdin 是斷開的
# （不斷開的話，「每月例行」的備份會停在沒人看的視窗上等輸入），所以密碼
# 只能靠環境變數傳進去——不在這裡問，從控制台跑的例行就永遠不會產生備份包。
# 留空＝這輪不備份：update 的備份那一步會以人話跳過，其餘照跑。
if ($env:TWCRAWL_BACKUP_PASSWORD) {
    Write-Host '備份密碼：沿用已設定的環境變數。' -ForegroundColor DarkGray
} elseif ([Console]::IsInputRedirected) {
    # 沒有主控台可問（從管線或測試啟動）。`Read-Host -AsSecureString` 讀的是
    # 主控台而不是 stdin，在這種情形會**直接卡住不動**——所以這裡不問，
    # 照樣往下走，只是這輪不會備份
    Write-Host '未設定備份密碼：每月例行會跳過備份那一步（其餘照跑）。' -ForegroundColor DarkGray
} else {
    # 整段包在 try 裡：備份密碼是選配的，問它的過程出任何差錯都不該讓
    # 控制台開不起來（$ErrorActionPreference = 'Stop' 會讓例外直接收掉腳本）
    $ok = $false
    try {
        $sec = Read-Host -AsSecureString '備份密碼（直接按 Enter 就跳過備份）'
        if ($sec -and $sec.Length -gt 0) {
            # SecureString 轉回明文才進得了環境變數；用完立刻釋放那段記憶體
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
            try {
                $env:TWCRAWL_BACKUP_PASSWORD = `
                    [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
            } finally {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            }
            $ok = $true
        }
    } catch {
        Write-Host "（問備份密碼時出錯：$($_.Exception.Message)）" -ForegroundColor DarkGray
    }
    if ($ok) {
        Write-Host '已設定備份密碼：控制台跑的「每月例行」會產生備份包。' -ForegroundColor DarkGray
    } else {
        Write-Host '未設定備份密碼：每月例行會跳過備份那一步（其餘照跑）。' -ForegroundColor DarkGray
    }
}
Write-Host ''

Write-Host '正在啟動 twcrawl 控制台…瀏覽器會自己打開。'
Write-Host ''
Write-Host '在頁面上按「每月例行」就會跑完整輪：登入 → 抓發票 → 食安清單 →'
Write-Host '比對 → 對獎 → 報表 → 備份。進度會即時顯示在頁面上。'
Write-Host '登入那一步會開瀏覽器要你本人操作（平台有圖形驗證碼，無法自動），'
Write-Host '完成後回頁面按「我已登入」就會繼續。'
Write-Host ''
Write-Host '這個視窗是伺服器本體：關掉它（或按 Ctrl+C）就整個停止。' -ForegroundColor DarkGray
Write-Host ''

& $twcrawl serve --control
$rc = $LASTEXITCODE

if ($rc -ne 0) {
    # serve 起不來的常見原因是跑錯目錄（工作區沒有資料庫），
    # twcrawl 自己會講人話，這裡只負責把視窗留著讓人看得到
    Write-Host ''
    Write-Host ('=' * 60) -ForegroundColor Yellow
    Write-Host "控制台沒能啟動（結束碼 $rc）。上面那段訊息說明了原因。" -ForegroundColor Yellow
    Write-Host ('=' * 60) -ForegroundColor Yellow
    Wait-Then-Exit $rc
}

# Ctrl+C／關視窗停止的是正常收工，不必留著擋路
exit 0
