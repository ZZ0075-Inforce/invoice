# 指令是可呼叫的函式，update 是步驟清單

`update` 是每月唯一會跑的指令，但它把七個 subcommand 的接線各重打了一遍：
七步裡有五步已經與對應的 subcommand 漂移（少了登入前置檢查、`since` 只給
FDA 沒給 match、沒有 `--no-cloud`、`max_pages=500` 兩份、備份密碼政策不同），
而步數還手打進八個 f-string。同時 15 個指令的 body 全部內嵌在 `main()` 裡，
覆蓋率約 4%。

我們決定：**每個指令是 `commands.py` 裡一個吃具名參數、回傳結果 dict、不印
摘要的函式；`update` 退化成一份 `Step` 清單，由 `run_steps` 執行。** `twcrawl
<cmd>` 與 update 的第 N 步呼叫同一個函式、同一份預設值，接線只有一份。

## Considered Options

- **只把 update 的七步抽出來**——解了漂移，但 `capture`／`ingest`／`handoff`
  的邏輯（最新目錄的選法、handoff 寫檔）仍鎖在 `main()` 裡測不到。否決。
- **全部 15 個指令一致抽出**——`main()` 可以變成純 dispatch table，但
  `serve`／`bizreg`／`geocode`／`probe` 的 body 只是 `return X.refresh(conn, …)`，
  包一層不會讓任何複雜度集中，刪掉它也不會有東西散開。否決，這四個留在
  `cli.py` 直接呼叫。
- **指令吃 argparse 的 Namespace**——省一次參數展開，但呼叫者必須知道
  namespace 裡有哪些欄位，等於沒有可讀的介面，而且 update 與測試都得偽造一個。
  否決。
- **統一吃一個 RunContext 物件**——步驟清單最整齊，但每個指令的介面都變成
  「ctx 裡的某些欄位」，隱性且會逐漸長成雜物袋。否決。

## Consequences

- **一步失敗記錄後續跑，`main()` 回非零退出碼。** 七步的產物都即時落地到
  資料庫，fetch 掛了之後幾步拿庫內既有資料重生仍然有意義（儀表板本來就有
  「已 N 天沒有新發票」的 staleness 橫幅會說明資料是舊的）；一個 FDA 來源
  暫時掛掉不該連帶砍掉對獎與儀表板。
- **`run_steps` 必須攔 `SystemExit`**——它是這個 codebase 的主要錯誤通道
  （`fda`、`einvoice_fetch`、`backup`、`bizreg`…），`except Exception` 攔不到。
- **人工中止改用 `KeyboardInterrupt`**（`browser.wait_for_operator` 原本拋
  `SystemExit`）。「這一步失敗」與「使用者按了 Ctrl+C」必須在型別上可分辨，
  否則中止登入之後 update 會若無其事地繼續跑 fetch。
- **跳過的步驟仍佔一個編號**，並印出跳過原因。原本 `--no-login` 完全不印、
  `--no-backup` 印一行說明，同樣是跳過卻有兩種呈現。
- **`since` 只給 feed 型的 FDA 來源。** 事件型清單（如中聯油脂案）不依日期
  遞減排序，它唯一含「日期」的欄位是產品的「有效日期」，與列表順序無關——
  拿 `since` 去停它會在半途任意截斷整份強制下架清單。型態因此從
  `export.FDA_SOURCE_META` 搬到 `fda.SOURCE_META`：抓取端要靠它決定
  `since` 適不適用，呈現端要靠它分頁，同一份知識兩邊都要。
  `_crawl_by_idx` 刻意沒有 `stop_when` 參數，別「補上」。
- **`match` 不吃 update 的 `since`**（維持原行為，但現在是明講的）：FDA 清單
  只回溯 90 天，比對本身卻該涵蓋全部發票——一張半年前的舊發票完全可能命中
  今天才公告的下架品。`twcrawl match --since` 留給使用者手動縮範圍。
- **備份密碼在組裝步驟時就問**，不是跑了一小時之後才卡在提示上。
- 路徑仍沿用既有慣例（`out_dir` 之類的具名預設參數），沒有集中的路徑物件；
  那是獨立的一件事，不在本 ADR 範圍。
