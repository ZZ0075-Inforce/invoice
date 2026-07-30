# Issue tracker: GitHub

本 repo 的議題與 PRD 都放在 GitHub Issues（`ZZ0075-Inforce/invoice`）。所有操作一律用 `gh` CLI。

## 慣例

- **建立議題**：`gh issue create --title "..." --body "..."`；多行內文用 heredoc。
- **讀取議題**：`gh issue view <number> --comments`，需要時用 `jq` 過濾留言並一併抓標籤。
- **列出議題**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，視情況加 `--label` 與 `--state` 過濾。
- **留言**：`gh issue comment <number> --body "..."`
- **加／移除標籤**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **關閉**：`gh issue close <number> --comment "..."`

repo 由 `git remote -v` 推斷——在 clone 內執行時 `gh` 會自動處理。
注意：本機有兩個 GitHub 帳號，寫入前先 `gh api user --jq .login` 確認（見 `.claude/CLAUDE.md`「已知的坑」）。

## Pull requests as a triage surface

**PRs as a request surface: no.** _（若本 repo 開始把外部 PR 視為功能請求則改成 `yes`；`/triage` 會讀這個旗標。）_

設為 `yes` 時，PR 走與議題相同的標籤與狀態，改用 `gh pr` 對應指令：

- **讀取 PR**：`gh pr view <number> --comments`，diff 用 `gh pr diff <number>`。
- **列出待 triage 的外部 PR**：`gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`，只留 `authorAssociation` 為 `CONTRIBUTOR`、`FIRST_TIME_CONTRIBUTOR` 或 `NONE` 者（去掉 `OWNER`/`MEMBER`/`COLLABORATOR`）。
- **留言／標籤／關閉**：`gh pr comment`、`gh pr edit --add-label`/`--remove-label`、`gh pr close`。

GitHub 的議題與 PR 共用同一組編號，`#42` 可能是其中之一——先 `gh pr view 42`，失敗再退回 `gh issue view 42`。

## 當 skill 說「publish to the issue tracker」

建立一個 GitHub issue。

## 當 skill 說「fetch the relevant ticket」

執行 `gh issue view <number> --comments`。

## Wayfinding 操作

供 `/wayfinder` 使用。**map** 是單一議題，其 **child** 子議題作為 tickets。

- **Map**：標上 `wayfinder:map` 的單一議題，內文放 Notes / Decisions-so-far / Fog。`gh issue create --label wayfinder:map`。
- **Child ticket**：以 GitHub sub-issue 連到 map 的議題（`gh api` 打 sub-issues 端點）。sub-issues 不可用時，把 child 加進 map 內文的 task list，並在 child 內文開頭放 `Part of #<map>`。標籤：`wayfinder:<type>`（`research`/`prototype`/`grilling`/`task`）。被認領後把 ticket 指派給負責的 dev。
- **Blocking**：用 GitHub **原生 issue dependencies**——canonical、UI 可見的表示法。加依賴邊：`gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`，其中 `<blocker-db-id>` 是 blocker 的**數字 database id**（`gh api repos/<owner>/<repo>/issues/<n> --jq .id`，_不是_ `#number` 也不是 `node_id`）。GitHub 回報 `issue_dependencies_summary.blocked_by`（只算 open 的 blocker——即現行門檻）。dependencies 不可用時，退回在 child 內文開頭放一行 `Blocked by: #<n>, #<n>`。所有 blocker 都關閉即解除封鎖。
- **Frontier query**：列出 map 的 open children（`gh issue list --state open`，範圍限 map 的 sub-issues / task list），去掉仍有 open blocker（`issue_dependencies_summary.blocked_by > 0`，或 `Blocked by` 行裡仍 open 的議題）或已有 assignee 者；照 map 順序取第一個。
- **Claim**：`gh issue edit <n> --add-assignee @me`——session 的第一個寫入動作。
- **Resolve**：`gh issue comment <n> --body "<answer>"`，接著 `gh issue close <n>`，再把 context 指標（gist + 連結）附到 map 的 Decisions-so-far。
