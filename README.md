# twcrawl

**以個人電子發票為底的長期消費資料庫**：自動累積自己的消費紀錄（SQLite），
供各種分析使用；FDA 問題商品比對是其中一種分析。

四件事：

1. **抓自己的電子發票消費紀錄** — 人工登入財政部電子發票整合服務平台一次，之後指定區間逐月全自動抓取
2. **消費分析五頁** — 月報（分類堆疊／趨勢／預算／消費日曆）、查詢頁、食安頁、年度回顧、消費地圖，全部本機、雙擊即開
3. **統一發票對獎** — 傳統獎項＋雲端發票專屬獎（官方 PDF 清冊），每月例行順帶比對
4. **FDA 問題商品比對** — 強制下架清單、國內回收公告、國際警訊三來源；店家、品項、警訊標題三層級，結果在食安頁與報告 CSV

背景與路線評估見 [**可行性評估文件**](docs/feasibility-einvoice.md)。結論是官方 API 自 112/3/31 起不再受理個人申請（需 ISO27001／CNS27001），因此改走人工登入 + 網路層擷取。

---

## 安裝

**Windows（PowerShell）**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
python -m playwright install chromium
```

**Windows（命令提示字元 cmd）**

```bat
python -m venv .venv
.venv\Scripts\activate.bat
pip install -e .
python -m playwright install chromium
```

**macOS / Linux**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python3 -m playwright install chromium
```

> **一定要用 `pip install -e .`**：PyPI 上有個同名但完全無關的套件（2019 年的 Twitter 爬蟲），`pip install twcrawl` 會裝錯東西。
>
> 每開新的終端機視窗都要重新 activate 虛擬環境，否則會找不到 `twcrawl` 指令。
>
> 若 PowerShell 因執行原則擋下 `Activate.ps1`，可改用
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` 後重試，或直接用 cmd 的
> `activate.bat`。不想 activate 也可以直接呼叫 `.venv\Scripts\twcrawl.exe`。

若環境已有 Chromium 但版本與 Playwright 不符，用環境變數指定執行檔位置：

```powershell
$env:TWCRAWL_CHROMIUM = "C:\Program Files\Google\Chrome\Application\chrome.exe"   # PowerShell
```

```bat
set TWCRAWL_CHROMIUM=C:\Program Files\Google\Chrome\Application\chrome.exe        :: cmd
```

---

## 日常流程

**平常這樣用：雙擊工作區裡的 `twcrawl-console.bat`**，瀏覽器會開出控制台頁，
按「每月例行」就跑完整輪，進度即時顯示在頁面上；登入那一步會開瀏覽器要你本人
操作，完成後回頁面按「我已登入」即可繼續。不必開終端機、不必 activate venv。
詳見下面的[一鍵啟動](#一鍵啟動不開終端機)與[控制台](#控制台從頁面按按鈕跑工作)。

終端機這條路完全保留，兩邊做的是同一件事。每月例行只要一個指令：

```powershell
twcrawl update      # 登入 → fetch → fda → match → 對獎 → export（＋加密備份包）
```

或者分步驟跑，只有第一個需要動手：

```powershell
twcrawl login                              # 人工登入（僅在憑證失效時，約數小時一次）
twcrawl fetch --from 2026-01 --to 2026-07  # 逐月自動抓發票＋品項明細
twcrawl fda --since 2026-03-01             # 更新問題商品清單
twcrawl match --since 2026-03-01           # 比對，輸出 out\match_report.csv
twcrawl lottery                            # 統一發票對獎（傳統獎＋雲端專屬獎）
twcrawl export                             # 衍生消費分析五頁（跑完自動開啟，--no-open 關）
```

### 1. `login` — 人工登入

開啟瀏覽器視窗，**由你自己完成登入**（消費者身分 + 手機號碼 + 驗證碼 + 圖形驗證碼，或自然人憑證／行動自然人憑證）。完成後回終端機按 Enter；從控制台跑的話則是回頁面按「我已登入」。

登入 cookie 存到 `state/einvoice.json`（權限 600、已列入 `.gitignore`）。工具**不會讀取也不會儲存你的密碼**。

平台的認證憑證有效期約數小時，`fetch` 回報登入失效時再重跑一次即可。

> 終端機不可互動時（AI 代理在背景啟動、或工作是控制台頁按下去的）改用訊號檔收尾：
> 你只操作瀏覽器，另一端建立該檔案即代表完成。控制台的「我已登入」按的就是這個機制。
>
> ```powershell
> $env:TWCRAWL_DONE_FILE = "$env:TEMP\twcrawl_done.flag"   # PowerShell
> ```
> ```bat
> set TWCRAWL_DONE_FILE=%TEMP%\twcrawl_done.flag           :: cmd
> ```

### 2. `fetch` — 逐月自動抓取

```powershell
twcrawl fetch --from 2026-01 --to 2026-07     # 含品項明細
twcrawl fetch --from 2026-07 --to 2026-07     # 只抓單月（補抓新資料）
twcrawl fetch --from 2026-01 --to 2026-07 --no-details   # 只抓發票表頭，較快
```

平台 UI 一次只能查一個月，但那是前端限制——API 沒有，所以本指令把區間切成月份逐月呼叫，你只要給起訖。

比人工操作更完整：查詢會涵蓋**所有載具**（人工在 UI 只能一次查一個），且逐張抓品項明細，不需要匯出 CSV。

抓完自動解析入庫。單月失敗不會中止整批（最後會列出未取得的月份，可單獨重跑該月）；登入失效則立即停止並提示重跑 `login`。

**資料涵蓋範圍的先天限制**（與程式無關，是制度本身）：只涵蓋已存入載具的雲端發票，紙本與未出示載具的消費不會出現；悠遊卡、一卡通等載具需先歸戶至手機條碼。

**查詢窗口很窄，所以「定期抓」不是可選項**（2026-08-01 實測）：平台對太舊的起始日一律拒絕（HTTP 400，訊息是查詢區間異常），連表頭都拿不到——不是「更早只剩表頭」。實測當日只能查到當年 1 月 1 日為止，且單次區間必須落在同一個曆月內。**平台不是典藏處，這個資料庫才是**：沒抓下來的月份日後補不回來（只剩檢具身分證、載具影本與申請書向平台專案申請一途），所以請把 `twcrawl update` 當成每月例行，並定期 `backup`。

#### `import` — 匯入手上的 CSV（歷年資料唯一入口）

```powershell
twcrawl import "C:\Users\你\Downloads\匯出檔.csv"
```

上面那條硬限制的另一面：`fetch` 抓不到的月份，只能靠**你手上已經有的檔案**補。
不論來源是以前手動匯出的 CSV、還是向平台專案申請取得的檔案，這道指令解析後
入庫，接著 `twcrawl export` 重生報表就看得到。

- 沿用既有的 CSV 解析與編碼後備（`utf-8-sig` / `utf-8` / `big5hkscs` / `cp950`），
  含表頭的新版寬表與舊版 M/D 列型都吃
- **重跑同一個檔案不會長出重複列**（upsert 以發票號碼為主鍵，冪等）
- 只印筆數，不印金額與店家名
- 略過的列會**明講幾列**（頁尾／統計列屬正常）——靜默丟棄的話，欄位對不上的
  檔案會「匯入成功」卻只進一半，而這條路一年跑不到一次，等看報表少一整年才
  發現就太遲了。同理，解不出品項的列也會明講：寬表是一列一品項，「明細 0 列」
  多半代表品名欄名對不上，而不是這個檔案真的沒有明細
- 檔案不存在、空檔、只有表頭、認不得的格式，各有各的人話訊息與下一步

> 目前只支援**已知的平台 CSV 格式**。若你的檔案匯不進去，工具會說它讀到幾列幾欄
> 但找不到發票號碼；要讓它支援新格式，提供**第一列的欄位名**就夠了（不要任何值）。

### 3. `fda` — 問題商品清單

```powershell
twcrawl fda                                # 全部三個來源
twcrawl fda --since 2026-03-01             # 新聞式清單回溯到此日期即停止翻頁
twcrawl fda --source edible_oil            # 只抓單一來源
twcrawl fda --headed                       # 想看瀏覽器在做什麼
```

| 來源 | 內容 |
|---|---|
| `edible_oil` | 中聯油脂案強制性下架產品下游業者清單（約 5,100 筆、257 頁） |
| `csm_news` | 國內衛生局新聞（回收、下架、稽查公告） |
| `csm_light` | 國外消費紅綠燈（國際回收警訊） |

後兩者是依日期排序的新聞清單，**務必加 `--since`**，否則會一路翻到十年前。

以列內容雜湊去重，重跑只更新 `last_seen`，不會產生重複資料。

### 4. `match` — 比對

```powershell
twcrawl match --since 2026-03-01
```

三個層級同時比對，結果印在畫面上並輸出 `out\match_report.csv`
（同步呈現在食安頁 `out\fda.html`，依事件／監測來源分頁）：

| 層級 | 意義 | 怎麼判讀 |
|---|---|---|
| 店家 × 問題業者 | 你在「榜上有名的通路」消費過 | **不代表買到問題商品**，只代表該通路曾有產品下架 |
| 品項 × 問題產品 | 發票品項名稱對上下架產品名稱 | 名稱愈具體愈可信；通用菜名（炒飯、豬排）多為巧合 |
| 品項 × 回收/警訊標題 | 品項名稱出現在回收公告標題中 | 一律標示「需人工確認」，字面命中而已 |

比對做了全形轉半形與空白正規化，並排除純數字品項（價格、重量）等必然誤報。

### 5. `export` — 消費分析五頁（月報、查詢頁、食安頁、年度回顧、地圖）

```powershell
twcrawl export            # 跑完自動用預設瀏覽器開啟月報
twcrawl export --no-open  # 只產出，不開瀏覽器
```

從資料庫衍生五頁到 `out\`（含 `data.js`——品項與發票號碼只存在這個本機檔）：

- **`dashboard.html` 月報**——每月支出（依店家分類堆疊）、分類趨勢（一分類
  一格的小倍數折線，看「某分類是不是越來越多」）、消費日曆（每日熱力格）、
  分類累計、店家排行、非必要消費近期清單、FDA 比對命中卡、統一發票對獎磚
  （有中獎會出現明細卡）；設定了預算（見下）會多出預算磚。月柱、分類、店家
  列點下去會帶著條件跳到查詢頁；右上角亮暗主題切換（各頁共用記憶）。
- **`query.html` 查詢頁**——關鍵字搜尋（店家／品名／發票號碼）＋月份、分類、
  金額篩選，發票逐張展開品項（含發票狀態；中獎的列標 🎉、非常態狀態在號碼旁
  標註）；「店家查詢」看單一店家的月趨勢與品項統計；「固定支出」自動偵測
  訂閱型消費（同店家、金額相近、月級以上週期、至少 3 次）。
- **`fda.html` 食安頁**——FDA 比對獨立呈現，不混在消費月報裡：總覽（各來源
  資料量、命中數、三層級判讀基準）＋依來源自動分頁——「中聯油脂案」是事件區，
  「國內回收公告」「國際警訊」是常設監測區。未來出現新食安事件，只要在
  `fda.py` 的 SOURCES 加一條來源，這頁自動長出新分頁，UI 不用改。
- **`year.html` 年度回顧**——日曆年至今的全貌：年度統計（總額、月均、非必要
  佔比、對獎戰績）、亮點（單筆最大消費、最貴的一天）、分類全年佔比（與月報
  同一套顏色）、店家排行。12 月後重跑 `export` 就是完整年度版。
- **`map.html` 地圖**——見下方 `geocode`。

頁面完全本機、不外連（[`docs/adr/0002`](docs/adr/0002-static-view-serve-writeback.md)；
地圖頁的 OSM 圖磚是唯一例外）。

店家分類走兩層規則：常見連鎖與業種通用詞內建於工具；個人常去的小店寫在
工作區根目錄 `categories.local.json`（已 gitignore），格式如下，改完重跑 `export` 即生效：

```json
{
  "rules": { "小巷麵館": "餐飲", "拾光": "咖啡" },
  "unnecessary": ["手搖飲", "甜點零食", "咖啡"]
}
```

儀表板底部會列出金額最高的未分類店家，照著補規則即可。`unnecessary` 決定
哪些分類算「非必要消費」（選填，預設：手搖飲、甜點零食、咖啡）。
`aliases` 則把公司登記名對應到招牌名（例：`"拾光": "DAYLIGHT COFFEE"`），
儀表板顯示招牌名、公司名放 tooltip。

**預算（選填）**：工作區根目錄放 `budget.local.json`（已 gitignore、金額只在
本機）就會多出預算磚——每月總額與非必要消費上限皆選填、可只設其一；所有
月份一律以現行預算對照，磚上點名失守的月份。完全沒設定就完全沒有磚：

```json
{ "monthly": 25000, "unnecessary": 3000 }
```

**整理分類最省力：`twcrawl serve`**。起一個只綁本機（127.0.0.1）的小站開同一套
頁面——未分類清單與查詢頁的店家查詢會多出「存檔」按鈕，填好分類直接寫回
`categories.local.json` 並重生資料，不用手改 JSON；雙擊開檔的 file:// 模式則提供
「複製規則片段」。規則檔手貼壞了也不怕：語法錯（含行號）、重複鍵、型別錯都給
人話提示（雙模式設計見 [`docs/adr/0002`](docs/adr/0002-static-view-serve-writeback.md)）。

#### 控制台：從頁面按按鈕跑工作

`serve` 之下還多一頁**控制台**（各頁右上的「🎛 控制台」進得去，或雙擊
`twcrawl-console.bat` 直接開）。五顆按鈕：

| 按鈕 | 做的事 |
|---|---|
| **每月例行** | 整輪 `update`（登入 → 抓發票 → 食安清單 → 比對 → 對獎 → 報表 → 備份） |
| **登入** | 單獨重登（`fetch` 回報登入失效時用） |
| **抓發票** | 指定起訖月份的 `fetch`，逐月進度即時顯示 |
| **匯入** | 貼上 CSV 路徑跑 `import`，**跑完自動重生報表**並給出可點的連結 |
| **重生報表** | `export` |

輸出即時顯示在頁面上；某步失敗時其餘照跑、結尾有彙總，頁面看得出是哪一步。
進行中可以**中止**，連底下的瀏覽器一起收掉，不留半死的行程。
登入那一步會停下來等你——頁面出現「請在開啟的瀏覽器完成登入」與「我已登入」，
按下去工作才繼續（平台有圖形驗證碼，這一步任何介面都消不掉）。

工作跑在子行程，所以改過程式碼也不必重啟 serve 才生效。同時只跑一個工作；
別的分頁按下時這頁會接上去看同一份進度，不會誤報成失敗。

匯入那顆要的是**檔案路徑**而不是選檔視窗：瀏覽器只會給檔名不給路徑，真要走
選檔就得把整個檔案上傳、再落地一份到某個暫存目錄——那份是你的消費紀錄，多一
份就多一個要記得刪的地方。貼路徑則一個位元組都不必搬。在檔案總管按
**Shift＋右鍵** →「複製檔案路徑」，貼進去就好（連引號一起貼也沒關係）。

> **備份那一步需要密碼，而控制台不收密碼**（頁面收密碼要多一條傳輸與多一個
> 存放的地方，不值得）。改由**啟動器在開站時問一次**：`twcrawl-console.bat` 會問
> 「備份密碼（直接按 Enter 就跳過備份）」，輸入後只放進這個視窗的環境變數，
> 控制台起的每個工作都繼承得到——所以按下「每月例行」就會產生備份包。
> 留空也照樣啟動，只是那一步會以人話跳過（其餘照跑）。
> 已經自己設好 `TWCRAWL_BACKUP_PASSWORD` 的話就直接沿用，不再問。

**想更精確：`twcrawl bizreg`**（財政部稅籍登記公開資料，66MB、官方每月更新，
加 `--force` 重新下載）。只保留你發票出現過的統編，之後 `export` 自動獲得三件事：
未分類店家用稅籍**行業別**後備歸類（rules 沒中才用）、地圖連結帶**營業地址**、
tooltip 與未分類清單顯示行業和常買品項——判斷「這家到底是什麼店」一眼就有答案。

**地圖檢視：`twcrawl geocode` → 開 `out\map.html`**。geocode 把稅籍營業地址
一次性轉成座標（NLSC 門牌定位為主、OSM Nominatim 路段級後備，限速 1 req/s，
增量、重跑安全）。地圖頁以圓點標示消費地點（顏色＝分類、大小＝金額），
可用「全部／近三月／近一月」或自訂月份區間篩選；圖例可點選隱藏分類；
搜尋框輸入店家名即時過濾圓點（與圖例、時間篩選取交集，清空即恢復），
自動完成選單選定店家則直接定位；圓點的 popup 有「查這家」直達查詢頁的
店家查詢。
注意：**地圖頁開啟時會載入 OpenStreetMap 圖磚**（主儀表板維持零外連）；
查詢的地址是公開商工登記資料，消費資料本身不出去。

### 6. `lottery` — 統一發票對獎

```powershell
twcrawl lottery                # 傳統獎項＋雲端發票專屬獎
twcrawl lottery --offline      # 用庫內存過的號碼離線重對
twcrawl lottery --no-cloud     # 跳過雲端專屬獎清冊（首抓全部獎別約 247MB）
```

抓官方開獎號碼（本期＋上期）比對庫內發票：傳統獎項（特別獎～六獎、增開
六獎）比末 8 碼；雲端發票專屬獎（百萬／2,000／800／500 元）比完整字軌號碼
——號碼來自官方 PDF 清冊（五百元獎單期即 119MB），抽出後以 gz 快取、同期
不重下。同張同時符合兩類依規定擇高。
結果進月報的對獎磚與中獎明細卡（含領獎期限），查詢頁的發票列標 🎉。
中獎受領獎期限約束，過期不再比對。

### 7. `backup` / `restore` — 加密備份包與還原

```powershell
twcrawl backup                             # 互動輸入密碼，或設 TWCRAWL_BACKUP_PASSWORD
twcrawl restore out\backup\twcrawl-backup-20260802-0029.zip
```

備份包是 AES-256 加密 zip（產在 `out\backup\`），內容是資料庫、`captures\`
與個人設定（`categories.local.json`、`budget.local.json`）——**只有這個檔案
可以放雲端硬碟**。`state\`（登入 cookie）永遠不進備份，還原時也不會憑空出現，
所以換機之後要重跑一次 `login`。密碼自行保管，遺失即不可讀。

平台的查詢窗口只到當年年初（見上面 `fetch` 那節），**本機資料毀損就是永久
遺失**——請定期備份，並把備份包放到雲端硬碟或另一顆磁碟。

`restore` 會把包解回**目前所在的目錄**，然後當場打開資料庫報出發票與明細
筆數、最新發票日期，確認資料真的回來了。工作區已經有資料時它會直接拒絕
（訊息會列出哪些東西會被蓋掉），確定要覆蓋才加 `--force`。密碼錯、檔案不在、
不是備份包這幾種情形都給人話訊息，而且**一個檔案都不會寫出去**。

#### 換到新電腦

1. 依上面「安裝」裝好 Python 環境（venv + `pip install -e .` + `playwright install chromium`）
2. 建一個**空目錄**當新工作區，並 `cd` 進去（工作區就是你所在的目錄）
3. 把雲端硬碟上的備份包抓下來，還原：

   ```powershell
   twcrawl restore D:\Downloads\twcrawl-backup-20260802-0029.zip
   ```

   輸入備份密碼，看它印出「發票 N 張、明細 M 列、最新發票日期 …」——數字對得上就成功了。
4. `twcrawl export` 產生五頁確認畫面正常（分類規則與預算都在包裡，不必重建）
5. `twcrawl login` 重新登入平台（登入 cookie 不在備份包內），之後 `twcrawl update` 照舊

> 這條路實際演練過（2026-08-02）：真實資料庫備份 → 空目錄還原 → `export`
> 五頁全數產出，筆數與原工作區一致。

### 8. `update` — 每月例行一鍵跑完

```powershell
twcrawl update                # 登入 → fetch（自動補到當月）→ fda → match → lottery → export → backup
twcrawl update --no-login     # 憑證還有效時跳過登入
twcrawl update --no-backup    # 不產備份包
twcrawl update --no-open      # export 後不自動開儀表板
twcrawl update --no-cloud     # 對獎時跳過雲端專屬獎清冊（首抓全部獎別約 247MB）
```

fetch 區間自動推算：從資料庫最新發票的月份（重抓補漏，寫入冪等）到當月；
FDA 回溯 90 天（只對回收公告、國際警訊這類依日期排序的來源有效；事件型的
專屬下架清單一律整份擷取）。

七步各自獨立：**某一步失敗會記錄下來繼續跑下一步**，結尾列出哪幾步沒過，
退出碼非零。每一步的產物都即時寫進資料庫，所以就算抓取失敗，後面的比對與
儀表板拿既有資料重生仍然有意義（儀表板會顯示「已 N 天沒有新發票」）。
按 Ctrl+C 則會中止整輪。

#### 一鍵啟動（不開終端機）

工作區裡的 **`twcrawl-console.bat`** 雙擊就會起本機小站並開啟**控制台頁**，
每月例行從那裡按——不必先開終端機、也不必 activate venv。

啟動時會問一次**備份密碼**（直接按 Enter 就跳過備份）。密碼只放進這個視窗的
環境變數、不落地，控制台起的工作都繼承得到，所以「每月例行」按下去就會產出
加密備份包（在 `out\backup\`，自己要記得放一份到雲端）。

那個黑色視窗**就是伺服器本體**：跑工作的時候不要關，用完關掉視窗（或按 Ctrl+C）
就整個停止。起不來的話（例如放錯目錄、工作區還沒有資料庫）視窗會留著顯示原因，
按 Enter 才關。

放到桌面的做法：在 `twcrawl-console.bat` 上按右鍵 →「傳送到」→「桌面（建立捷徑）」。
捷徑不論放哪裡都以**這個檔所在的目錄**當工作區，所以不會跑錯地方。

> 這個檔在 2026-08-02 之前叫 `twcrawl-update.bat`（雙擊直接跑 update）。舊捷徑會
> 失效，重新建一次即可。

> 實作上是一層 ASCII 的 `.bat` 殼呼叫 `twcrawl-console.ps1`。原因是 cmd.exe 會用
> 主控台的 OEM codepage（繁中 Windows 為 cp950）逐位元組解析批次檔，UTF-8 的中文
> 會被切成假指令、結束碼還會錯回 0；`chcp` 救不了，因為壞在解析階段。中文訊息
> 因此全放在 PowerShell 那一半。

---

## 維護與除錯指令

平台 API 改版、或解析結果不如預期時使用。

```powershell
twcrawl capture     # 人工操作瀏覽器，錄下平台回傳的所有回應
twcrawl handoff     # 產生可安全分享的去值化摘要
twcrawl ingest      # 重新解析最新一次擷取（解析規則改了不必重抓）
twcrawl ingest --dir captures\einvoice-20260727-000848
twcrawl probe <url> # 頁面結構偵察報告（表格 id、表頭、分頁連結、XHR）
```

`capture` + `handoff` 是校正欄位的標準流程：錄下真實回應後，`handoff` 會印出**有哪些端點**與**回應的欄位名稱與型別**，所有值（含 token）都代換成型別名稱，因此輸出可以直接貼給別人看。依實際欄位名補上 `src/twcrawl/sites/einvoice.py` 的 `ALIASES` 即可，流程不需改動。

---

## 產出

指令一律在**工作區**內執行——工作區就是你所在的目錄，底下所有路徑都由它推出
（沒有全域設定檔，也沒有指定路徑的旗標）。跑錯目錄時，讀取型指令會直接說
「這裡不是 twcrawl 工作區」，而不是默默生一份空的儀表板。

| 位置 | 內容 |
|---|---|
| `out\twcrawl.sqlite` | `invoices` / `invoice_items` / `fda_rows` / `biz_registry` / `lottery_draws` |
| `out\dashboard.html` + `out\query.html` + `out\fda.html` + `out\year.html` + `out\data.js` | 月報、查詢頁、食安頁、年度回顧（雙擊即開、離線） |
| `out\map.html` + `out\vendor\` | 消費地圖（開啟時載入 OSM 圖磚） |
| `out\match_report.csv` | 比對結果（UTF-8 BOM，Excel 可直接開） |
| `out\fda_*.csv` | 問題商品清單 |
| `out\backup\` | AES-256 加密備份包（唯一可上雲的產物；含資料庫、`captures\` 與個人設定） |
| `out\bgmopen1.zip` | 稅籍登記資料快取（公開資料，非個資） |
| `categories.local.json` | 個人店家分類規則與招牌名別名 |
| `budget.local.json` | 個人預算設定（選填；每月總額與非必要上限） |
| `captures\` | 平台原始回應（**你的消費紀錄**） |
| `state\` | 登入 cookie（**等同帳號本身**） |

`captures\`、`state\`、`out\`、`*.sqlite` 全部列在 `.gitignore`，不會進版控。

> **不要把 `captures\` 或 `state\` 的內容傳給任何人**，包含請 AI 協助除錯時。
> 需要分享時一律用 `twcrawl handoff` 產生的去值化摘要。

---

## 測試

```powershell
python tests\test_twcrawl.py                    # 69 個測試，不需要 pytest
python tests\test_twcrawl.py --update-golden    # 有意改動畫面後重生頁面快照
```

表格擷取與分頁以本機模擬的 ASP.NET 頁面驗證（含「分頁點了沒反應」與「下一頁提前消失」兩種真實壞掉情境）；解析器、比對、日期邊界、摘要遮蔽則以實際資料形狀驗證。測試會實際啟動 headless Chromium，需先完成 `playwright install`。

五個頁面以**合成 payload**（`a_payload()`）在 headless Chromium 裡實際渲染，斷言零 JS 錯誤、店家名不會被當 HTML 執行、殘缺的 data.js 不會讓整頁空白；畫面結構與樣式存成 `tests/golden/*.txt` 快照，有意改動畫面後跑 `--update-golden` 重生並審閱 diff。合成 payload 的形狀由 `test_payload_contract` 與 `export.build_payload` 對齊。互動面（趨勢圖 tooltip、預算磚、狀態顯示、地圖搜尋、色槽取色）各有專屬測試。

控制台的長工路徑另有四支：登入交接（子行程印出的標記 → 按鈕 → 訊號檔 → 子行程繼續，兩端同一份定義）、中止（真的造一棵行程樹，確認孫行程也被收掉——只殺父的話留下的就是還開著的 Chromium）、工作端點本身（真的起子行程跑 `export` 與 `fetch`，順帶釘住白名單、參數形狀與跨來源防護），以及匯入的完整鏈（真的匯入一個 CSV，斷言同一個工作接著把報表重生、`data.js` 裡出現剛匯入的店家）。

---

## 設計說明

- **不依賴 CSS selector 取資料。** 電子發票平台是 SPA，資料一定走 XHR，因此直接呼叫 API；FDA 那側是傳統 ASP.NET 頁面，用通用表格擷取，並以內容雜湊確認真的有翻頁成功。
- **原始資料一律保留。** `invoices.raw`、`fda_rows.data` 與 `captures/` 存整筆原始內容，解析規則之後要改都不必重抓。
- **所有寫入都是 upsert。** 以發票號碼／列內容雜湊為鍵，重跑安全、可隨時中斷續跑。
- **登入永遠由人工完成。** 不做圖形驗證碼破解——個人記帳一個月跑一次，人工介入數十秒的成本可忽略，同時避免帳號被平台判定為異常行為。
- **憑證不落地。** 平台的 bearer token 放在瀏覽器 sessionStorage，`fetch` 在頁面內就地取用並加上請求標頭，不寫入檔案、不進 Python 記憶體。
- **本機為正、雲端只存加密備份。** 消費紀錄是敏感個資：儀表板是純本機靜態檔、不外連；只有 `backup` 產出的 AES-256 密文可以上雲（[`docs/adr/0001`](docs/adr/0001-local-first-encrypted-cloud-backup.md)）。

專案的完整技術細節（API 端點形狀、站台實測事實、踩過的坑）記錄在 [`.claude/CLAUDE.md`](.claude/CLAUDE.md)。
