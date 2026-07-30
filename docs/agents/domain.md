# 網域文件（Domain Docs）

工程類 skill 探索本 codebase 時，應如何取用網域文件。

## 探索前先讀

- repo root 的 **`CONTEXT.md`**
- **`docs/adr/`**——讀與即將動工區域相關的 ADR

檔案不存在就**靜默略過**：不要提示缺檔、不要主動建議先建立。`/domain-modeling`（經 `/grill-with-docs`、`/improve-codebase-architecture` 觸發）會在術語或決策真正定案時惰性建立。

## 檔案佈局

本 repo 是 single-context：

```
/
├── CONTEXT.md
├── docs/adr/
│   └── 0001-….md
└── src/
```

（若日後拆成 multi-context，改在 root 放 `CONTEXT-MAP.md` 指向各 context 的 `CONTEXT.md`，並重跑 `/setup-matt-pocock-skills`。）

## 用詞遵循詞彙表

輸出中出現網域概念時（議題標題、重構提案、假說、測試名稱），一律用 `CONTEXT.md` 定義的術語，不要滑向詞彙表刻意避免的同義詞。

需要的概念不在詞彙表裡，就是個訊號——不是你在發明專案沒有的語言（該重想），就是真的有缺口（記下來給 `/domain-modeling`）。

## ADR 牴觸要明講

輸出與既有 ADR 牴觸時，明確標出而非默默推翻：

> _與 ADR-0007（event-sourced orders）牴觸——但值得重開，因為…_
