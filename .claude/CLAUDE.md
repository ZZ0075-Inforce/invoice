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
python tests/test_twcrawl.py          # 測試（54/54，不用 pytest）
python tests/test_twcrawl.py --update-golden   # 有意改動四頁畫面後重生 tests/golden/

# 每月例行（一鍵；fetch 區間自動推算、FDA 回溯 90 天只給 feed 型來源）
twcrawl update                        # login→fetch→fda→match→lottery→export→backup
                                      #   一步失敗記錄後續跑、結尾彙總、回非零退出碼
                                      #   --no-login/--no-backup/--no-open/--no-cloud

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
- **路徑一律由工作區推出**（2026-07-30）：`Workspace(Path.cwd())` 是唯一來源，
  沒有 `--db`、沒有 `--root`、沒有路徑環境變數，模組也不再有 CWD 錨定的常數
  （`DEFAULT_DB`／`BACKUP_DIR`／`CACHE_DIR`／`LOCAL_RULES_PATH`／`STATE_DIR`／
  `MATCH_REPORT` 全刪；`export.TEMPLATE` 是 package-relative，留）。要換工作區
  就 cd 過去。單一路徑的函式吃那條路徑（簽章才說得出它碰什麼），多路徑的
  （export／serve／backup）吃 ws。詞彙見 CONTEXT.md「工作區」。

## 架構

```
src/twcrawl/
├── workspace.py  工作區：所有本機路徑的單一來源（CONTEXT.md「工作區」）。
│                 Workspace(root)——CLI 傳 Path.cwd()、測試傳 tmpdir，這兩個
│                 adapter 就是 root 必須是參數的理由（以前測試只能 os.chdir）。
│                 除了路徑，也收佈局知識：ensure_out/new_capture（命名＋建
│                 responses/downloads，capture 與 fetch 共用一份）／
│                 latest_capture（依 mtime，不是字典序——字典序會讓
│                 einvoice-fetch-* 恆勝 einvoice-2*）／require_db（讀取型
│                 指令的前置：不是工作區就講人話，不默默生空資料庫）。
│                 這個模組不 import 其他 twcrawl 模組
├── cli.py        argv 解析＋dispatch＋格式化，指令內容不在這（見 commands.py）；
│                 建 ws＝Workspace(cwd) 往下傳，_db 保證連線關閉
├── commands.py   指令層（ADR-0003）：每個指令一個吃 ws 與具名參數、回傳 dict、
│                 不印摘要的 cmd_* 函式，`twcrawl <cmd>` 與 update 的第 N 步呼叫
│                 同一個、接線只有一份；update = Step 清單＋run_steps（一步
│                 失敗記錄後續跑、跳過仍佔編號、SystemExit 要攔而
│                 KeyboardInterrupt 不攔）。純委派的 serve/bizreg/geocode/probe
│                 刻意留在 cli.py 直接呼叫——包一層不會讓複雜度集中
├── browser.py    Playwright session、wait_for_operator（pump！、中止拋
│                 KeyboardInterrupt 好與「步驟失敗」分辨）、storage_state；
│                 session 以檔案路徑傳入，這個模組不知道工作區在哪
├── capture_index.py  `captures/<目錄>/index.json` 的單一定義：Entry（NamedTuple）
│                 ＋ Index（seq、隨錄隨寫 flush、路徑相對化）＋ 容錯讀取。
│                 兩個寫入端（netcapture 錄真實瀏覽、einvoice_fetch 重放 API）
│                 與兩個讀取端（ingest 對檔、handoff 印摘要）都經過它。
│                 **JSON 欄位名不可更動**——captures/ 是重新解析的來源，明細
│                 只保存近半年，寫出不相容的索引等於把舊擷取變成廢紙
│                 （test_capture_index_is_backward_compatible 把關）。
│                 這個模組不 import 其他 twcrawl 模組、不碰 Playwright
├── netcapture.py 攔截 XHR/下載 → captures/（索引交給 capture_index）
├── tables.py     通用表格擷取 + 分頁（含截斷警告）
├── db.py         SQLite schema 與 upsert（全部冪等，重跑安全——這句由 `_require_key`
│                 守著：upsert 只取自己認得的鍵，鍵名打錯會被靜默丟掉而寫出一列
│                 NULL 主鍵，且 NULL 不受主鍵唯一性約束，重跑幾次就累積幾列）。讀取面**只收
│                 不只一個 caller 要的**：`invoices()`／`invoice_items()` 吃同一組
│                 過濾（since／months）並回傳 NamedTuple（解包與屬性都能用）——
│                 過濾條件由 `_invoice_where(alias)` 出一份，join 版與單表版同源，
│                 match 以前是把單表版字串 `.replace('inv_date','v.inv_date')`。
│                 `invoices()` 保證 inv_date 非空且依日期升冪（儀表板的「最新發票」
│                 與比對報告的列序都靠它；以前不帶 since 時沒有 order by，拿到的是
│                 SQLite 儲存順序）。amount 的非空保護留在 export——只有它在加總。
│                 `biz_registry()`／`upsert_biz()`／`set_biz_location()`：五個表裡
│                 最後一個補上 helper 的（schema 與 migration 本來就在這，讀寫卻
│                 散在 bizreg／geocode）；upsert 刻意不碰座標欄，重抓對照表不該
│                 洗掉 geocode 解出的結果。`seller_industries()`（店家名→稅籍行業，
│                 統編取該店家名下任一非空；半數發票不帶統編，逐張查會讓同一家店
│                 在不同發票得到不同分類）。**一次性的聚合查詢留在原地**（FDA 來源
│                 統計、常買品項 top3、max(inv_date)）——搬進來只是換個地方放 SQL
├── match.py      發票 × FDA 比對（店家/品項/警訊標題三層級；FDA 欄位名以關鍵字
│                 自動定位。兩道精確化（2026-07-28 使用者回饋）：①品項/警訊層級
│                 濾除餐飲現調店家——菜名撞包裝品名屬誤報（菜名×同名即食包），
│                 判定＝Category.eatery，與儀表板同一條鏈（2026-07-30 前是雙路
│                 OR，會讓店家規則已判定的便利商店/百貨被稅籍翻成餐飲而濾掉）；
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
├── categories.py 店家分類，**整條優先序鏈收在單一介面之後**（2026-07-30；之前
│                 export 與 match 各自組鏈且組法不同）。介面只有兩個呼叫：
│                 for_seller(名) / for_invoice(名, 品項) → Category(name, source,
│                 unnecessary, eatery)。鏈：item_rules 品項覆寫（source=item，改
│                 「整張發票」，跨業態店家用——好市多加油發票→加油，店家業態不動；
│                 通用層只收無鉛汽油/柴油，曖昧詞放個人層）→ rules 個人（personal）
│                 → rules 通用（generic，連鎖＋業種詞）→ 稅籍行業 INDUSTRY_RULES
│                 後備（industry）→ 未分類（none）。同層長樣式優先；稅籍多重行業
│                 （「餐館、咖啡館」）逐段比對、**主業優先**——bizreg 保留財政部的
│                 主業在前順序，整串一起掃會被次要業別攔截。
│                 **「沒命中」看 source=="none"，不要拿名字比 UNCATEGORIZED**。
│                 稅籍行業靠 with_industries() 接上——Classifier 常在拿到 conn 之前
│                 就建好（serve、測試），所以由 build_payload/run_match 統一接，
│                 少接不會報錯只會讓兩成店家靜默掉回未分類。
│                 aliases 招牌名別名（沒命中回原名，不是 None）；
│                 unnecessary 預設手搖飲/甜點零食/咖啡，個人層**整組取代**；
│                 eatery 預設餐飲/速食/手搖飲/咖啡，個人層**聯集**（現調是事實不是
│                 偏好，內建清不掉；自創分類名如「麵食」要在這宣告否則濾除靜默失效）；
│                 normalize() 公開給 match 共用，別再各留一份；
│                 load_local_config 防呆——語法錯（含行號）/重複鍵/型別錯
│                 都給人話而非 traceback
├── bizreg.py     財政部 BGMOPEN1 稅籍登記（66MB zip 串流過濾，只留自己的統編入
│                 biz_registry；欄位以關鍵字定位；Py3.13 需關 VERIFY_X509_STRICT——
│                 政府憑證缺 SKI）
├── export.py     四頁衍生（out/data.js＋**複製整個 web/ 目錄**——ui.js 是四頁的必要
│                 相依，逐檔明列時漏一個會從「少一頁」變成「四頁都壞但看起來像沒
│                 資料」；file:// 不能 fetch JSON 所以走 script src；
│                 店家附行業/地址/常買品項 top3；發票列含品項全欄位與發票號碼——
│                 載具號碼、raw 永不進 data.js，ADR-0002；fda.sources 歸戶：match
│                 報告的 source 欄是 source_url，反查 SOURCES 得名稱，FDA_SOURCE_META
│                 給顯示名/型態、未知來源後備「名稱＋事件」；_detect_fixed 固定支出
│                 偵測也在這——月報磚與查詢頁視圖同源；load_budget 讀
│                 budget.local.json（monthly／unnecessary 皆選填正數，未知鍵
│                 直接報錯——只有兩個合法鍵，打錯字被靜默忽略磚會無聲消失；
│                 沒設定→payload 無 budget 鍵→無磚，錯誤訊息只印鍵名不印
│                 金額值）；分類色槽 assign_slots
│                 在這指派並持久化 state/catslots.json（在榜沿用、新進取空槽、
│                 槽滿只收落榜者），payload 帶 categories[].slot，頁面只讀——
│                 跨次匯出同分類同色，未分類永遠中性灰）
├── serve.py      本機小站（ADR-0002 雙模式；只綁 127.0.0.1）：靜態服務 out/ ＋
│                 POST /api/rules 併規則入 categories.local.json→重生 data.js；
│                 ThreadingHTTPServer，每請求自開 SQLite 連線（執行緒安全）
├── backup.py     AES-256 加密備份包（pyzipper；state/ 有防呆 assert 永不進包）
├── geocode.py    地址→座標：NLSC TextQueryMap 門牌級為主（**要帶 maps.nlsc.gov.tw
│                 的 Referer** 否則 PERMISSION DENIED）、Nominatim 路段級後備（台灣
│                 門牌會 MISS）；稅籍地址須清洗（全形、里鄰、截到「號」）
├── web/ui.js     四頁共用骨架。核心是 `TW.page({needs, ready, clear, render})`：
│                 主題套用（在 <head> 同步跑，否則先亮後暗閃一下）、payload 讀取與
│                 完整性判斷（needs 的鍵要存在，陣列還要非空）、**錯誤圍堵**——沒有
│                 這層的話 render 中途 throw 會留下已清空的容器，畫面與「找不到
│                 data.js」無法區分。順帶帶進 esc/nt/css/el/themeButton/catColors。
│                 **是全域 TW 不是 ES module**：file:// 下 import 走 CORS 會被擋，
│                 與 data.js 不走 fetch 同一個理由（ADR-0002）。別「現代化」
├── web/ui.css    四頁共用色票與文件版面。只收「在有定義的頁逐字相同、且沒定義的
│                 頁不受影響」的規則。「不受影響」要查兩條路徑：CSS 的 var(--x) 與
│                 **JS 的 getComputedStyle().getPropertyValue("--x")**（地圖的分類色
│                 就是後者取的），只查 var() 會漏。刻意留各頁：--warning（dashboard
│                 與 fda 值不一致）、--serious（只有 dashboard 且沒人用）、--event
│                 （只有食安頁用）、body/button.theme/h1（地圖是全視窗版面）
├── web/dashboard.html  月報模板（零相依 SVG 圖表、hover tooltip、亮暗切換鈕
│                 localStorage twcrawl-theme 三頁共用；分類趨勢＝小倍數折線，
│                 一分類一格、y 刻度各自獨立（分類金額差一個量級，共用刻度會把
│                 小分類壓成平線）；FDA 命中明細卡；非必要表
│                 只列近 10 筆其餘導查詢頁；調色盤 6 色槽過 dataviz 驗證器兩模式）
├── web/query.html 查詢頁（發票清單搜尋/篩選/逐張展開品項、店家查詢、固定支出偵測——
│                 同店家＋金額相近 ±max(15,5%)＋週期 25–400 天＋至少 3 次；
│                 深連結參數 ?seller/?cat/?from/?to/?unnecessary/?q/?view；
│                 店家查詢的排行/下拉由發票聚合而非 sellers[].category——
│                 品項覆寫後店家可跨分類，發票聚合才與磚的金額天生一致）
├── web/fda.html  食安頁（總覽：來源表＋三層級判讀基準；依來源自動分頁，
│                 事件/監測標示；深連結 ?src=<key>；月報只留磚、明細全在這）
├── web/year.html 年度回顧（日曆年至今：統計磚＋亮點卡＋分類佔比條＋店家
│                 排行。資料全來自 payload.year——年度＝庫內最新發票的年份
│                 不是牆上時鐘，統計與亮點由 build_payload 出、頁面只排版；
│                 佔比條 inline 取 cats.of 槽色，slot 錯開測試涵蓋本頁）
├── web/map.html  消費地圖（Leaflet vendored 不走 CDN；OSM 圖磚＝ADR-0001 明確例外；
│                 圓點色=分類、大小=金額；時間區間篩選＋圖例點選隱藏分類；
│                 popup「查這家」連查詢頁）
├── probe.py      頁面結構偵察
├── handoff.py    去值化摘要（URL query、POST 參數、JSON、CSV 全遮值；token 連「參數名」都遮；
│                 索引從 capture_index 讀——以前對 fetch 產物會把檔名字根印在端點欄位）
└── sites/
    ├── einvoice.py        ALIASES 欄位別名（2026-07-27 已依實測校正）、CSV/JSON/明細解析、ingest
    ├── einvoice_fetch.py  fetch 逐月重放 API（呼叫走頁內 fetch()，token 即取即用不落地；
    │                      _Sink 寫索引帶**真實 url 與 status**，回傳鍵是
    │                      fetched_invoices／fetched_details——以前叫 invoices，
    │                      被 ingest 回傳的 **res 靜靜蓋掉）
    └── fda.py             三來源下架/回收/警訊清單（?idx= 分頁優先，點擊後備）。
                           SOURCE_META 帶顯示名與事件/監測型態——抓取端靠它決定
                           since 適不適用、食安頁靠它分頁。**since 只給 feed 型
                           （監測）來源**：事件型清單不依日期排序，它唯一含
                           「日期」的欄位是產品「有效日期」，拿去停會任意截斷
                           整份下架清單，所以 _crawl_by_idx 刻意沒有 stop_when
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
- `invoiceStrStatus` 是狀態碼（INVOICE0003S…）。**`com001i/statusCodes/zh`
  字典實測只有 UI 訊息碼、沒有發票狀態碼**（2026-08-01 live 驗證 portal 與
  btc/cloud 兩份；此前記載有誤）——翻譯走 `export.INVOICE_STATUS_ZH` 靜態
  對照，只收實測可對應的碼，未收錄原樣顯示

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
- ✅ 分類解析收成單一 interface（2026-07-30，架構檢視 candidate 1）：原本
  `categories.py` 出四個獨立 resolver 卻不組裝，export 用後備鏈、match 用雙路
  OR，同零件組出不同結果。現在 `for_seller`／`for_invoice` → `Category`，鏈全在
  implementation 裡；`_EATERY_CATS` 手抄字串與 match 複製的 `_norm` 都刪除。
  重構本身**輸出零差異**（data.js 與 match_report.csv SHA256 相同）——實測三個
  不一致當時都還沒咬到資料。測試 34/34（品項覆寫與稅籍後備的語意從需要
  SQLite 檔改成純測試，export 端各留接線斷言）
- ✅ 稅籍多重行業改主業優先（2026-07-30，接在上一條後面單獨一個 commit，
  因為它會動輸出）：`industry_category` 原本整串一起掃 INDUSTRY_RULES 的插入
  順序，次要業別會攔截主業（「餐館、咖啡館」歸咖啡）。改成逐段比對、主業先問
  ——bizreg 本來就刻意保留財政部的主業在前順序。實測庫內 2 家店改分類、
  5 張發票換分類磚，非必要消費少 5 筆；match 報告 0 行差異。測試 35/35
- ✅ 指令層 commands.py（2026-07-30，架構檢視 candidate 3；決策見 ADR-0003）：
  15 個指令 body 從 `main()` 搬出 10 個，update 變成 Step 清單。修掉 update
  與 subcommand 的五處漂移（登入前置檢查、`--no-cloud`、max_pages 兩份、
  備份密碼政策、步數手打）；一步失敗改成記錄後續跑、回非零退出碼；人工中止
  改拋 KeyboardInterrupt 才分辨得出「Ctrl+C」與「步驟掛了」；`_latest_capture`
  改按 mtime 取（原字典序下 `einvoice-fetch-*` 恆勝 `einvoice-2*`）。
  FDA 的 `since` 改成只給 feed 型來源（事件型清單不依日期排序，拿去停會
  任意截斷；型態隨之從 export 搬進 `fda.SOURCE_META`）。CLI 表面向後相容，
  只新增 `update --no-cloud`。測試 35→40
- ✅ 工作區模組（2026-07-30，架構檢視 candidate 2；最後落地，吃下前兩支）：
  22 個散在 12 個模組的 CWD 相對字面路徑收進 `workspace.py`，`commands.py` 的
  `cmd_*` 改吃 ws；`--db` 移除、`backup --out` 保留（可指向雲端同步目錄）。
  讀取型指令先 `ws.require_db()`——跑錯目錄會講人話，不再默默生一份空儀表板。
  順帶修掉：`ingest` 印模組常數而非實際資料庫、備份包內路徑改相對工作區
  （Windows 磁碟機字母的退路因此不可達可刪）、測試 runner 只抓 `Exception`
  所以逃出的 `SystemExit` 會靜默中止整輪。測試 42/42（新增工作區與 CLI
  端到端各一；兩個 `os.chdir` 測試不再需要 chdir；備份測試補上 `state/`，
  ADR-0001 的紅線第一次真的被驗證）
- ✅ 四頁測試表面（2026-07-31，架構檢視 candidate 4 的步驟 1／共三步）：1,650 行
  頁面 JS 從零覆蓋變成有回歸網。關鍵是**合成 payload**——史上寫過十幾個煙霧
  腳本全都指向 `out/`，斷言帶真實金額與店家名，結構上進不了 repo 只能丟掉；
  頁面的 interface 是 `window.TWCRAWL_DATA` → DOM，資料庫不在其中，所以
  `a_payload()` 直接造，形狀由 `test_payload_contract` 與 `build_payload` 對齊
  （`fda.match`／`months[].byCategory` 是以領域值為鍵的對照表，不比鍵）。
  三個新測試：①六份 golden 結構快照（含 `?view=fixed`／`?cat=`／`?src=` 深連結
  變體，`--update-golden` 重生；`staleDays` 是唯一的牆上時鐘，正規化掉）
  ②惡意字串不進 DOM ＋ 殘缺 payload 不讓整頁空白 ③形狀契約。
  順帶修掉兩個附錄缺陷（先修才拍快照，否則零差異的基準是錯的）：dashboard
  **沒有 `esc`**（另外三頁都有）——店家/分類/行業/常買品項直接進 `innerHTML`，
  實測注入 9 個節點；`lot.next.drawDate` 無防護——null 會在 `app.innerHTML=""`
  之後 throw，畫面與「找不到 data.js」一模一樣。兩個修正都經「還原後測試必須
  變紅」驗證過。測試 42→45（Chromium 啟動 7→9）
- ✅ 抽 ui.js／ui.css（2026-08-01，candidate 4 步驟 2／3；行為零差異，快照把關）：
  四頁 2,168→1,886 行，共用 192 行。**過程中推翻了報告的前提**——「88 行逐字
  相同」是 dashboard↔query 的兩頁數字；四頁的規則層級交集只有 3 條（map 是全
  視窗 flex 版面，body/h1/header 全不同）。改以**宣告層級**抽色票才有價值。
  也因此先補了樣式探針：DOM 快照抓不到 CSS 迴歸，`@tokens`＋18 選擇器×10 屬性
  ×三種主題模式進 golden，抽離後只有 fda／map 的 `@tokens` 行變動（多了原本
  未定義的 token），`@style` 與 DOM 零變動。那些 token 逐一驗過**兩條引用路徑**
  （`var()` 與 JS 的 `getPropertyValue`）都沒人用，才收下。
  `export.write_export` 改複製整個 `web/`，`template=` 參數（無人使用、名字
  說謊）改名 `web_dir`。測試 45/45、42 秒
- ✅ 衍生歸一（2026-08-01，candidate 4 步驟 3／3；會動輸出）：界線是**誰決定它**
  ——被 Python 實作細節決定的事實由 Python 出，被使用者互動決定的分組留 JS。
  `_detect_fixed` 的門檻收成具名的 `FIXED_RULE`（minCount／tolAbs／tolPct／
  minDays／maxDays／staleFactor），期別標籤在 Python 算完（`fixed[].periodLabel`），
  門檻數字隨 `fixedRule` 進 payload 給查詢頁組文案——改門檻文案自動跟著改，
  而 query.html 那份重述門檻的對照表（最後一支「約 N 天」因為 Python 已濾掉
  med>400 而永遠不可達）整個消失。payload 拿掉 `unnecessary[]`（判準是
  `categories[].unnecessary` 旗標，頁面篩 invoices 即可；以前兩種編碼並存、
  月報用清單查詢頁用旗標）、`fda.match`（可從 matches 導出，而且它的 `{}` 在
  JS 是 truthy，會讓「有報告零命中」的磚從「—」翻成「0 筆」）、`lottery.uncovered`／
  `periods[].months`／`claimStart`（無人讀）。`ui.js` 收下 `unnecessaryCats`／
  `monthTotals`（查詢頁兩處逐字相同）／`monthBounds`（`-31` 上界的字串比較慣例
  原本散在兩頁）。**三份 seller rollup 刻意不收斂**——它們是三個不同的問題，
  且地圖那份的期間是互動參數，必然在客戶端（架構報告原本寫成「重複」，是誤判）。
  六份 golden 的畫面差異合計**只有一行**（固定支出說明多了實際門檻數字）。
  CONTEXT.md 的店家分類詞條同步改準：分析的分組單位是**發票的分類**不是店家
  ——品項覆寫上線後就不成立了，而同文件的非必要消費詞條早就寫對。測試 45/45
- ✅ db.py 讀取面（2026-08-01，架構檢視 candidate 5）：**範圍比報告寫的窄**——
  報告說收 `invoices_in_range()`，但實地看三個讀發票的地方，欄位重疊而**過濾
  條件是三件不同的事**（export 要非空、lottery 比月份、match 比 since），硬收
  成一個範圍讀取會變成參數化查詢建構器。改成共用**過濾器**而非共用查詢：
  `invoices()`／`invoice_items()` 吃同一組 since／months，回傳 NamedTuple。
  `match.py` 的 `cond.replace('inv_date','v.inv_date')` 因此消失（join 版與單表
  版由 `_invoice_where(alias)` 同源產生）。biz_registry 補齊 upsert／lookup／
  set_location，geocode 與 bizreg 不再手寫 SQL。原始 SQL：export 8→4、match 3→1、
  geocode 2→0、bizreg 2→1。**驗證**：真實資料的 payload SHA256 零差異；
  match 報告在 `--since` 那組逐位元組相同，不帶 since 那組**列相同、順序改變**
  ——舊碼在該路徑沒有 `order by`，拿到的是 SQLite 儲存順序（未定義行為），
  新碼一律日期升冪，已加測試釘住。順帶補上 COALESCE 不變式的測試（拿掉
  COALESCE 會紅，訊息是人話）。測試 45/45
- ✅ 擷取索引模組（2026-08-01，架構檢視 candidate 6）：`captures/<目錄>/index.json`
  的形狀本來是兩份手寫 dict literal，已經漂了——`_Sink` 硬寫 `status=200`、把
  合成標籤放進 `url`，於是 `handoff` 對 fetch 產物把檔名字根印在端點欄位。
  關鍵發現：**那不是「它不知道」**，真實的 url 與 status 就在呼叫點的 scope 裡，
  只是沒被傳進去。收進 `capture_index`（Entry／Index／read_entries／by_file），
  兩個寫入端與兩個讀取端都經過它。**JSON 欄位名一個字都沒動**——captures/ 是
  重新解析的來源、明細只保存近半年，寫出不相容的索引等於把舊擷取變成廢紙；
  相容性有測試把關（舊格式含下載項缺欄位、Windows 反斜線、壞檔），並實地用
  `twcrawl handoff` 對真實的 57 項舊目錄跑通。順帶修掉附錄缺陷 #7（`fetch_range`
  回傳 `{"invoices": total_inv, **res}`，`res` 也有 `invoices`，把抓取數靜靜蓋成
  入庫數）與一個死 import。`_Sink` 從「純檔案系統程式碼卻無從測起」變成有測試。
  測試 45→47
- ✅ 測試資料建構器（2026-08-01，架構檢視 candidate 7；**七張卡至此結清**）：
  42 個手打的資料庫字面（32 個 invoice、10 個 item——交接文件寫的 24／9 是低估）
  收成 `an_invoice()`／`an_item()`，參數名一律等於欄位名（名字不一致的話，
  多給的鍵會走進 `**extra` 再靠 dict 合併順序蓋掉位置參數，結果對是靠運氣）。
  **量測推翻了卡片的前提之一**：「`amount` int 與 float 並存」在 SQLite 的 REAL
  親和性下入庫都是 100.0，純屬觀感。真正有牙齒的是量測時挖出來的另一件事——
  `invoices.inv_num` 少了 `invoice_items` 早就宣告的 `NOT NULL`，於是鍵名打錯
  （`invNum`）會被 upsert 靜默丟掉、寫進一列號碼是 NULL 的發票，而 NULL 不受
  主鍵唯一性約束：**同一列 upsert 三次就是三列**，正好違反 db.py 開宗明義的
  「重跑安全」。生產端進不去（`einvoice` 有 `INV_NUM_RE` 擋，且只有 ingest 一個
  呼叫端），測試端則毫無防護。修法是 `_require_key`（兩個 upsert 都過；訊息印
  **鍵名不印值**，值是消費紀錄）＋ SCHEMA 補上 `NOT NULL`；**真正保護使用者那份
  資料庫的是前者**——`CREATE TABLE IF NOT EXISTS` 不會替既存表補約束，所以測試
  特地照舊 schema 先建一次表，走那條沒有 NOT NULL 的路徑（不這樣做，測試會被
  新 schema 的 IntegrityError 接住，看起來綠但沒驗到重點）。新增兩個測試：形狀
  三方釘住（表欄位 ↔ `_INVOICE_KEYS`／`_ITEM_KEYS` ↔ 建構器，連「常數有這個鍵
  但 INSERT 沒綁它」也抓得到）、以及守衛本身。順帶把只寫在註解裡的「`raw` 是
  COALESCE 的刻意例外」變成斷言。`fda_rows`／`lottery_draws` 的字面刻意不動——
  量測顯示它們各只有一種鍵組合，沒有漂移可收。**驗證**：四個修正逐一還原都確認
  變紅且訊息是人話；真實資料庫（485 張）的 payload SHA256 零差異；golden 未動。
  測試 47→49
- ✅ 分類色槽穩定指派（2026-08-01，issue #10）：色槽指派從 JS 的「當期金額
  排名取前六」搬到匯出端 `export.assign_slots`，持久化 `state/catslots.json`
  （工作區本機狀態、不進 repo 不進備份包）。規則：在榜沿用舊槽、新進者取
  最小空槽、槽滿只向落榜持有者收回（先收不在榜的、再收名次最低的）——
  排名變動不再讓整組跳色。payload 的 `categories[]` 加 `slot`（未分類恆
  None）；`ui.js` catColors 只讀 slot（舊 data.js 無 slot → 全灰、不炸），
  dashboard 兩處取色改走 cats.of/at、`SLOTS` 陣列刪除；map 零改動。首次
  指派＝舊排名順序，六份 golden 逐位元組零 diff；色票未動，dataviz 驗證器
  亮暗兩模式重跑全過（亮色 3 槽 <3:1 的 WARN 是既有狀態，月報有直接標籤
  與表格作 relief）。兩軸 code review 後補三件：`_load_slots` 擋 bool 與
  「未分類」佔槽（不變式不能被手改狀態檔繞過）、刪 `assign_slots` 無人用的
  `n_slots` 參數、新增「slot 與排名刻意錯開」的頁面取色測試——golden 的
  fixture slot 恰與排名重合，擋不住 ui.js 回歸成排名取色，這支直接斷言
  dashboard 與 map 圖例 swatch 落在指派槽色上（退回排名取色驗證過會紅）。
  測試 49→51
- ✅ 儀表板分類趨勢圖（2026-08-01，issue #11）：圖形式定案**小倍數折線**——
  問題是「單一分類的走向」，七系列疊同一張是麵條圖、堆疊面積中段讀不出
  個別走勢；一分類一格、格頭合計＋格上峰值、首尾月標籤，其他（含未分類）
  一格中性灰。y 刻度各格獨立（金額差一個量級）；顏色走 cats.at 同色槽；
  hover tooltip（月／分類／金額／佔當月%）、點格進查詢頁——「其他」格
  不給點（查詢頁表達不了「不在色槽內的那群」，連過去金額對不上）。
  純頁面改動、payload 未動；驗證器亮暗兩模式 ALL PASS（亮色 3 槽 <3:1
  WARN 既有，格頭直接標籤＋表格參照作 relief）；真實資料 7 格零 JS
  錯誤。新增 tooltip 內容測試（golden 拍不到事件驅動的 tooltip）＋單月
  不畫趨勢卡；樣式探針補 .trend 三選擇器。兩軸 review 後補修四件：
  探針、單月測試、「其他」格拿掉點擊、命名（.t→.head、mT2→padT）。
  測試 51→52
- ✅ 預算磚（2026-08-01，issue #12）：工作區放 `budget.local.json`
  （`{"monthly": 25000, "unnecessary": 3000}`，皆選填正數）→ 儀表板出
  預算磚；沒設定就沒有 payload 鍵、沒有磚（刻意不出空狀態提示，PRD
  story 5）。預算是分析視角不是契約：所有月份一律以現行預算對照，磚上
  本月 剩/超＋失守月份**點名**（「超總額：3月、5月」，>4 個月收成計數）。
  載入器在 export（`load_budget`），防呆比照規則檔（語法錯含行號、重複
  鍵、未知鍵、非正數；訊息只印鍵名不印值——金額是個資）；.gitignore 的
  categories.local.json 改成 `*.local.json` 一併蓋住。達成率由頁面以月
  合計＋非必要旗標組裝，匯出端只出設定值（衍生歸一界線）。兩軸 review
  後補修：FIXED_RULE 註解歸位、重複鍵防呆補上、只設上限時磚標籤改
  「本月非必要上限已用」（120% 不會被誤讀成總額超支）、失守月份由計數改
  點名、語法錯行號斷言放寬（尾逗號訊息 Py3.10~3.13 行號不同）。
  測試 52→54（載入器防呆＋磚三變體：雙預算 45%、只設上限 120%、
  無設定無磚）
- ✅ 年度回顧第五頁（2026-08-01，issue #13）：`out/year.html`——日曆年至今
  的統計磚（總額/月均/非必要佔比/對獎戰績）＋亮點（單筆最大、最貴的一
  天）＋分類佔比條（穩定色槽）＋店家排行前 10。`build_payload` 出
  `payload.year`（Python 決定的事實由 Python 出，頁面只排版）；**年度＝
  庫內最新發票的年份**不用牆上時鐘——一月還沒抓新資料不會出空回顧、
  golden 不吃當下日期；同額並列取最早（invoice_rows 日期升冪 + max 取
  首個）。儀表板 sub 加 📅 連結；五頁測試迴圈（PAGE_ROOTS）自動涵蓋
  惡意字串/殘缺 payload；slot 錯開測試延伸到佔比條（inline 取色，DOM
  快照拍不到）；golden 新增 year.txt。真實資料 13 分類/前 10 店家零
  JS 錯誤。兩軸 review 後補修：generatedAt 補 esc（dashboard 殘留那份
  一併修）、year.months→monthCount（與頂層 months 同名異義）、sub 文案
  改成兩種情形都為真（不寫「今年」）、對獎磚標「已開獎期別」、樣式註解
  改實話（抄的是 dashboard 變體）。測試 54/54（新頁走既有測試面）
- ✅ 發票狀態翻中文（2026-08-01，issue #14）：**PRD 的來源前提實測不成立**
  ——statusCodes 字典（portal 與 btc/cloud 都 live 驗過）只有 UI 訊息碼，
  沒有 INVOICE 狀態碼，公開網路也搜不到文件。改保守對照
  `export.INVOICE_STATUS_ZH`（只收實測可對應的 `INVOICE0003S`→開立；庫內
  431 張全部命中、54 張 CSV 舊來源無狀態），未收錄碼原樣進 payload 不吞
  資訊。翻譯在匯出端做完（invoices[].status），「算不算常態」也是——
  payload 帶 statusFlagged（_STATUS_NORMAL 單一定義），頁面只讀旗標不
  解讀譯文（拿譯文比對的話，改譯名會讓 431 列靜默全長出徽章）。查詢頁：
  非常態才在號碼旁標（開立不佔列上版面）、展開明細一律顯示狀態行；惡意
  測試補「點開全部發票列」讓狀態行與品項表的跳脫被實際渲染；DOM 面測試
  釘住三個顯示決策。不需重抓（inv_status 已入庫）。測試 54→55
- ⬜ 緩辦（要做先問）：CSV 匯出（分類趨勢圖已做＝#11；地圖店家搜尋立案為 #15）
- ⬜ 使用者待辦：持續補 categories.local.json 規則（儀表板未分類清單現在附
  稅籍行業與常買品項，好判多了）；跑一次 `twcrawl backup` 並把備份包放上 Google Drive
- ⬜ 未決：要不要回溯更早月份（明細僅保存近 6 個月，更早只抓得到表頭）
- ⬜（可選）statusCodes 狀態碼翻中文；FDA 開放資料的「回收藥品資料集」可另接（食品類無資料集）；
  品項層級分類／價格追蹤（等品名正規化有解）；儀表板分類顏色跨次匯出的穩定對應
