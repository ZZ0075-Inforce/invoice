# twcrawl — 個人消費資料庫（電子發票長期典藏＋分析）

**定位：以個人電子發票為底的長期消費資料庫，供各種分析使用；FDA 問題商品比對是
其中一種分析。**（2026-07-27 自「毒油比對工具」升級；詞彙見 `CONTEXT.md`，
隱私架構見 `docs/adr/0001`：本機為正、雲端只存加密備份、明文永不上雲。）
手段：人工登入＋網路層擷取取得**個人**電子發票，SQLite 為唯一正式儲存層。
Python 3.10+、Playwright、SQLite。

FDA 目前接三個來源：中聯油脂案專區強制下架清單（edible_oil）、國內衛生局回收/下架
公告（csm_news）、國外消費紅綠燈國際警訊（csm_light）。match 以關鍵字在每列 JSON 中
自動定位欄位，新增來源不需改比對邏輯；食安頁（fda.html）依來源自動分頁——
事件（專屬清單）與常設監測（公告 feed）分開標示，未知來源後備為「名稱原文＋事件」。

## 指令速查

```
pip install -e .                      # 一律 editable；PyPI 上的 twcrawl 是無關的舊套件
python -m playwright install chromium # playwright 指令找不到時用這個
python tests/test_twcrawl.py          # 測試（31/31，不用 pytest）

# 每月例行（一鍵；fetch 區間自動推算、FDA 回溯 90 天）
twcrawl update                        # login→fetch→fda→match→lottery→export→backup

# 分步驟
twcrawl login                         # 人工登入（token 約數小時失效，失效才需重跑）
twcrawl fetch --from 2026-01 --to 2026-07   # 逐月全自動抓發票＋明細（不碰 UI）
twcrawl fda --since 2026-03-01        # 更新問題商品清單（三來源，不需登入）
twcrawl match --since 2026-03-01      # 比對，輸出 out/match_report.csv
twcrawl lottery                       # 對獎：傳統獎＋雲端專屬獎（首抓清冊約 247MB 入快取）
                                      #   --offline 免連網、--no-cloud 跳過雲端清冊
twcrawl export                        # 衍生四頁 out/：dashboard+query+fda+map；自動開頁，--no-open 關
twcrawl serve                         # 本機小站（127.0.0.1）：同頁面＋歸類寫回 categories.local.json
twcrawl bizreg                        # 財政部稅籍對照表（統編→行業/地址；66MB 公開資料）
twcrawl geocode                       # 地址→座標（NLSC 門牌級為主；增量）→ out/map.html
twcrawl backup                        # AES-256 備份包（唯一可上雲產物；state/ 永不進包）

# 維護／除錯
twcrawl capture                       # 人工操作、錄下回應（API 改版時用來重新確認形狀）
twcrawl ingest                        # 重新解析最新擷取入庫（解析規則改了不用重抓）
twcrawl handoff                       # 產生可分享的去值化摘要（值全部代換成型別）
twcrawl probe <url>                   # 頁面結構偵察報告
```

`login`/`capture` 需要互動：由代理（Claude）背景啟動時，設 `TWCRAWL_DONE_FILE=<路徑>`
改為訊號檔收尾——使用者只操作瀏覽器、在對話說完成，代理建立該檔案即收工。實測這是最穩的流程。

## 已定案的決策（不要重新討論）

- **不走官方 API**：規範限企業組織＋ISO27001，個人無資格。評估見 `docs/feasibility-einvoice.md`。
- **不破圖形驗證碼**：登入一律人工（每月幾十秒），避免帳號被判異常。
- **不依賴 CSS selector**：電子發票是 SPA → 攔截 XHR；FDA 是 ASP.NET → 通用表格擷取。
- **登入後的逐月查詢可自動化**：重放 API（端點形狀見下），不走 UI 點擊。
- **倉庫個資界線**（2026-07-29 定案；07-30 起倉庫 public、歷史自乾淨快照
  重啟）：repo 檔案、commit 訊息與 issues 不寫消費金額、中獎明細、完整地址、
  個人店家名（範例一律假名；連鎖名當通用設計範例可）與「使用者買了◯◯」級
  的實例；純筆數統計可。推送前掃 `origin/main..HEAD` 的全部 diff 與 commit
  訊息。本地分支 `private-history` 含個資舊歷史，**永不推向任何遠端**。

## 架構

```
src/twcrawl/
├── cli.py        指令進入點
├── browser.py    Playwright session、wait_for_operator（pump！）、storage_state
├── netcapture.py 攔截 XHR/下載 → captures/（隨錄隨寫 index.json）
├── tables.py     通用表格擷取 + 分頁（含截斷警告）
├── db.py         SQLite schema 與 upsert（全部冪等，重跑安全）
├── match.py      發票 × FDA 比對（店家/品項/警訊標題三層級；FDA 欄位名以關鍵字
│                 自動定位。兩道精確化（2026-07-28 使用者回饋）：①品項/警訊層級
│                 濾除餐飲現調店家——菜名撞包裝品名屬誤報（菜名×同名即食包），
│                 判定走店家分類＋稅籍行業兩路（_EATERY_CATS）；
│                 ②店家層級做品項排除——發票品項與該業者名下下架產品全無交集
│                 就排除（純採買通路不會因上榜而命中），無明細才留純通路提示。
│                 排除/濾除數與樣本照印保持透明）
├── lottery.py    統一發票對獎（invoice.etax.nat.gov.tw 靜態頁：本期在首頁、上期
│                 lastNumber.html；期別由 tfoot「領獎期間」反推——頁面期別字樣兩期
│                 都有不可信；頭獎號碼拆相鄰 span、剝 tag 要用空字串才黏得回；
│                 傳統獎比末 8 碼。雲端專屬獎比完整字軌：cloudNow/LastNumber 頁
│                 抓「已排序」PDF 清冊（五百元獎 119MB 百萬組）、pypdf 抽號碼
│                 存 out/cache/cloud_*.txt.gz（期別取自 PDF 檔名、PDF 用完即刪、
│                 同期檔名固定不重下）；同張兩類都中依規定擇高（also 註記）。
│                 結果即算即得不落地，號碼存 lottery_draws 表供離線重對）
├── categories.py 店家分類：rules 兩層（通用連鎖＋業種詞內建；個人店家在
│                 categories.local.json，gitignored、個人優先、長樣式優先）→ 稅籍行業
│                 INDUSTRY_RULES 後備 → 未分類；aliases 招牌名別名（登記名→招牌名）；
│                 item_rules 品項覆寫（發票層級）：品項命中就改「整張發票」的分類
│                 ——跨業態店家用（好市多加油發票→加油，店家業態不動；油品發票
│                 實測張張單品項所以覆寫整張即精確），通用層只收無鉛汽油/柴油，
│                 曖昧詞放個人層；非必要分類預設手搖飲/甜點零食/咖啡；
│                 load_local_config 防呆——語法錯（含行號）/重複鍵/型別錯
│                 都給人話而非 traceback
├── bizreg.py     財政部 BGMOPEN1 稅籍登記（66MB zip 串流過濾，只留自己的統編入
│                 biz_registry；欄位以關鍵字定位；Py3.13 需關 VERIFY_X509_STRICT——
│                 政府憑證缺 SKI）
├── export.py     四頁衍生（out/data.js＋複製模板；file:// 不能 fetch JSON 所以走 script src；
│                 店家附行業/地址/常買品項 top3；發票列含品項全欄位與發票號碼——
│                 載具號碼、raw 永不進 data.js，ADR-0002；fda.sources 歸戶：match
│                 報告的 source 欄是 source_url，反查 SOURCES 得名稱，FDA_SOURCE_META
│                 給顯示名/型態、未知來源後備「名稱＋事件」；_detect_fixed 固定支出
│                 偵測也在這——月報磚與查詢頁視圖同源）
├── serve.py      本機小站（ADR-0002 雙模式；只綁 127.0.0.1）：靜態服務 out/ ＋
│                 POST /api/rules 併規則入 categories.local.json→重生 data.js；
│                 ThreadingHTTPServer，每請求自開 SQLite 連線（執行緒安全）
├── backup.py     AES-256 加密備份包（pyzipper；state/ 有防呆 assert 永不進包）
├── geocode.py    地址→座標：NLSC TextQueryMap 門牌級為主（**要帶 maps.nlsc.gov.tw
│                 的 Referer** 否則 PERMISSION DENIED）、Nominatim 路段級後備（台灣
│                 門牌會 MISS）；稅籍地址須清洗（全形、里鄰、截到「號」）
├── web/dashboard.html  月報模板（零相依 SVG 圖表、hover tooltip、亮暗切換鈕
│                 localStorage twcrawl-theme 三頁共用；FDA 命中明細卡；非必要表
│                 只列近 10 筆其餘導查詢頁；調色盤 6 色槽過 dataviz 驗證器兩模式）
├── web/query.html 查詢頁（發票清單搜尋/篩選/逐張展開品項、店家查詢、固定支出偵測——
│                 同店家＋金額相近 ±max(15,5%)＋週期 25–400 天＋至少 3 次；
│                 深連結參數 ?seller/?cat/?from/?to/?unnecessary/?q/?view；
│                 店家查詢的排行/下拉由發票聚合而非 sellers[].category——
│                 品項覆寫後店家可跨分類，發票聚合才與磚的金額天生一致）
├── web/fda.html  食安頁（總覽：來源表＋三層級判讀基準；依來源自動分頁，
│                 事件/監測標示；深連結 ?src=<key>；月報只留磚、明細全在這）
├── web/map.html  消費地圖（Leaflet vendored 不走 CDN；OSM 圖磚＝ADR-0001 明確例外；
│                 圓點色=分類、大小=金額；時間區間篩選＋圖例點選隱藏分類；
│                 popup「查這家」連查詢頁）
├── probe.py      頁面結構偵察
├── handoff.py    去值化摘要（URL query、POST 參數、JSON、CSV 全遮值；token 連「參數名」都遮）
└── sites/
    ├── einvoice.py        ALIASES 欄位別名（2026-07-27 已依實測校正）、CSV/JSON/明細解析、ingest
    ├── einvoice_fetch.py  fetch 逐月重放 API（呼叫走頁內 fetch()，token 即取即用不落地）
    └── fda.py             三來源下架/回收/警訊清單（?idx= 分頁優先，點擊後備）
```

## 站台實測事實（2026-07 校正）

**共通**：gov.tw 的 WAF 擋 `HeadlessChrome` UA——`browser.py` 已在 headless 時自動換一般 UA。

**FDA**（`fda.gov.tw/EdibleOilOperator/index.aspx`）：
- 共 257 頁、每頁固定 20 筆（無筆數下拉）、無官方整份下載檔
- 「下一頁」連結會在最後兩頁前提前消失 → 偵測「共 N 頁」+ `?idx=N` 連結時改按頁數走訪

**電子發票**（資料端點在 `service-mc.einvoice.nat.gov.tw`）：
- **認證：`Authorization: Bearer <sessionStorage.token>`**（1178 字元）。token 由 SPA 開機時
  取得，**不在 cookie 內**（`document.cookie` 是空的），Playwright 的 storage_state 也不會
  保存 sessionStorage——所以只帶 cookie 呼叫 API 一律 401。`fetch` 的作法是在頁面內就地
  讀 sessionStorage 加標頭，token 不落地、不進 Python 記憶體
- `btc502w/getSearchCarrierInvoiceListJWT`（POST）→ 查詢用 token。參數：
  `{cardCode:"", carrierId2:"", searchStartDate, searchEndDate, invoiceStatus:"all",
  isSearchAll:"true"}`；日期為 24 字元 UTC ISO（台北月初 00:00 = 前月最後一天 16:00Z）。
  `isSearchAll:"true"` 會涵蓋**所有載具**，比 UI 手動查單一載具完整
- `btc502w/getCarrierList`、`btc502w/searchCarrierInvoice?page=N&size=50`（POST `{"token":…}`，
  Spring Data 分頁殼：totalPages/content[]）。**每列自帶 272 字元 token**，直接拿去換該張
  明細，因此自動化完全不需要 CSV 匯出
- `common/getCarrierInvoiceData`/`getCarrierInvoiceDetail`：單張表頭/明細，**回應不含發票號碼**
  ——號碼在 POST 的 JWT payload 且不對齊 base64 邊界（`_jwt_invoice_number` 以 4 偏移解）
- CSV 匯出：`triggerInvoiceDetailExport` → `downloadInvoiceDetailCSV/<id>`；新版格式為
  UTF-8 BOM 含表頭寬表、一列一品項、品項欄掛 `消費明細_` 前綴（舊 M/D 格式保留後備解析）
- `invoiceDate` 是 UTC ISO 時間戳（`…T16:00:00Z` = 台北隔日）——`_norm_date` 已 +8 轉台北日期
- `invoiceStrStatus` 是狀態碼（INVOICE0003S…）；code→中文對照在 capture 到的
  `com001i/statusCodes/zh`，尚未使用

## 關鍵教訓（改 browser/netcapture 前必讀）

**Playwright 同步 API 的事件處理器只在主執行緒進入 Playwright 呼叫時派發。**
等待人工操作時卡在 `input()`/`time.sleep()` → 網路攔截整段失效、什麼都錄不到。
`wait_for_operator(pump=page)` 用 `wait_for_timeout` 持續抽水，回歸測試
`test_wait_for_operator_pumps_events` 把關。另外：事件處理器內不要做會長時間阻塞的事；
XHR 一律錄下不看 content-type（政府網站常把 JSON 標成 text/plain）。

## 🔒 隱私紅線

絕不讀取、絕不請使用者提供：`captures/**/responses/*.json`（實際消費紀錄）、
`captures/*/index.json`（POST body 含手機條碼）、`state/einvoice.json`（登入 cookie）。
一律用 `twcrawl handoff` 的去值化摘要。這些目錄都在 `.gitignore`。

## 已知的坑

- **serve 是長駐進程：改任何 Python（尤其 categories.py 規則）後必須重啟 serve**，
  否則頁面存檔觸發的重生用的是進程內舊模組，會把新規則的效果蓋回去
  （2026-07-29 實際發生：交通三分被舊 serve 重生蓋掉）
- Windows 測試：SQLite 連線必須在 TemporaryDirectory 清理前 `close()`（否則 PermissionError）
- 測試會真的啟動 headless Chromium，一輪約 1～2 分鐘，需先 `playwright install chromium`
- venv 每開新終端機要重新 activate；Chromium 版本不符設 `TWCRAWL_CHROMIUM` 指向現有執行檔
- 明細保存期約近 6 個月；表頭可回溯前 7 年；載具需先歸戶
- 久未操作後 `fetch` 回 401 是 token 正常過期，重跑 `login` 即可，不是 bug
- 推送前先確認 `gh` 帳號與 git 認證一致（本機有兩個 GitHub 帳號，git 走 Windows
  認證管理員而非 `gh` 設定，曾因不一致被 403）：`gh api user --jq .login`

## Agent skills

### Issue tracker

議題追蹤走 GitHub Issues（`ZZ0075-Inforce/invoice`，用 `gh` CLI）。見 `docs/agents/issue-tracker.md`。

### Triage labels

採預設五標籤（needs-triage、needs-info、ready-for-agent、ready-for-human、wontfix）。見 `docs/agents/triage-labels.md`。

### Domain docs

Single-context：root `CONTEXT.md` + `docs/adr/`。見 `docs/agents/domain.md`。

## 目前狀態（2026-07-28）與下一步

- ✅ FDA 三來源共 7,169 筆入庫：edible_oil 5,139、csm_news 260、csm_light 1,770
  （後兩者以 `--since 2026-03-01` 回溯；日期停止條件正常）
- ✅ 電子發票：`fetch` 全自動抓齊 2026-01～07，共 485 張發票、1,231 列明細入庫
  （已涵蓋問題油案起始的 3–4 月；明細品質維持 0 筆「商品一批」）
- ✅ `twcrawl match` 兩道精確化（2026-07-28，使用者接連回饋菜名誤報、通路
  空洞命中）：①餐飲現調濾除（濾 8 筆，全屬菜名撞包裝品名——原「兩筆值得追」
  即此型，取消）②店家層級品項排除（四家通路共 10 筆全排除——品項皆不在
  該業者下架清單，把前次人工逐一確認自動化）。報告 19 筆噪音 → 1 筆（零售保留）
- ✅ 2026-07-27 grilling 定案並實作完成（詳見 CONTEXT.md、docs/adr/0001）：
  消費分析儀表板 v1（`export`，兩檔制）、店家分類兩層規則、`update` 一鍵例行、
  `backup` 加密備份。實測 485 張全數入庫呈現，加規則後未分類金額大幅收斂
- ✅ 店家精確化三件套（2026-07-27）：aliases 招牌名、常買品項 top3 提示、
  `bizreg` 稅籍對照表（78/79 統編命中；行業後備分類讓未分類再減半）
- ✅ 消費地圖（2026-07-27）：`geocode` 77/77 門牌級座標入庫、`out/map.html`
  時間區間篩選；無統編的店家上不了地圖（頁底有清單），屬已知限制
- ✅ UI 全能查詢工具 M1「查」（2026-07-28，方向與雙模式見 docs/adr/0002）：
  data.js 品項＋發票號碼、query.html 三視圖（搜尋篩選展開／店家查詢／固定支出偵測）、
  月報深連結（月柱/分類/店家/非必要）、export 與 update 自動開頁（--no-open 關）
- ✅ M2「改」（2026-07-28）：`twcrawl serve` 同頁雙模式——file:// 唯讀＋複製規則
  片段、serve 之下未分類清單與店家查詢「存檔」寫回並自動重生 data.js；規則檔防呆
- ✅ M3「磨」（2026-07-28）：月報非必要表收斂（近 10 筆＋查詢頁連結）、FDA 命中
  明細卡（match 明細進 payload、報告路徑跟 out_dir）、亮暗切換鈕（三頁共用
  localStorage）；地圖圖例點選篩選、popup「查這家」連查詢頁。UI 三階段全數完成
- ✅ 食安頁獨立（2026-07-28）：FDA 比對搬出月報成第四頁 fda.html（總覽＋依來源
  自動分頁、事件/監測標示；月報只留磚連過去）；match 品項命中補標 source（原為
  空字串，來源歸戶靠它）；新事件＝fda.py SOURCES 加一條，UI 零改動
- ✅ 對獎功能（2026-07-28，研究熱門發票 app 後定向——對獎是發票怪獸/發票存摺/
  CWMoney 的第一功能）：`twcrawl lottery` 抓官方號碼比對庫內發票、update 納入
  第 5 步。UI 同輪完成：data.js lottery payload（不含號碼本身）、月報第六磚
  ＋中獎明細卡（有中才出現、含領獎期限）、查詢頁發票列 🎉 標記（tooltip 獎別）
- ✅ 雲端專屬獎比對（2026-07-28，使用者指出缺口後補）：PDF 清冊→gz 快取→
  完整字軌交集，五百元獎實測 385 萬組/期（77,000 頁 PDF，pypdf 解壓上限
  要放寬到 512MB）；同張兩類擇高（also 註記）。**實測中 2 筆**（獎別／
  店家／領獎期限見月報中獎卡）；測試 31/31、連結圖 28 網址全綠
- ✅ 品項覆寫（2026-07-29，使用者問跨業態店家的加油發票怎麼歸類後定案）：
  發票層級第三種規則——品項命中 item_rules（內建無鉛汽油/柴油→加油，
  categories.local.json 可擴充）就改整張發票分類，店家業態分類不動；
  查詢頁店家排行/下拉改由發票聚合，跨分類店家與磚的金額一致。實測庫內
  油品發票全數改歸加油；測試 32/32。完整品項層級分類（每品項各自歸類）
  維持緩辦，等品名正規化有解
- ⬜ 緩辦（要做先問）：CSV 匯出、分類趨勢圖、地圖店家搜尋
- ⬜ 使用者待辦：持續補 categories.local.json 規則（儀表板未分類清單現在附
  稅籍行業與常買品項，好判多了）；跑一次 `twcrawl backup` 並把備份包放上 Google Drive
- ⬜ 未決：要不要回溯更早月份（明細僅保存近 6 個月，更早只抓得到表頭）
- ⬜（可選）statusCodes 狀態碼翻中文；FDA 開放資料的「回收藥品資料集」可另接（食品類無資料集）；
  品項層級分類／價格追蹤（等品名正規化有解）；儀表板分類顏色跨次匯出的穩定對應
