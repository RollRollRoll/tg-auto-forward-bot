# Issue tracker：GitHub

这个仓库的 issue 与 spec 以 GitHub issue 的形式存在。**所有操作用 `gh` CLI。**

## 约定

- **创建 issue**：`gh issue create --title "..." --body "..."`。多行正文用 heredoc。
- **读 issue**：`gh issue view <number> --comments`，用 `jq` 过滤评论，并一并取回 labels。
- **列 issue**：
  `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，
  按需加 `--label` 与 `--state` 过滤。
- **评论**：`gh issue comment <number> --body "..."`
- **加 / 去 label**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭**：`gh issue close <number> --comment "..."`

通过 `git remote -v` 确认仓库；在克隆目录中运行时，`gh` 会使用当前仓库上下文。

## PR 作为 triage 入口

**PR 作为需求入口：否。** *（这个仓库要是把外部 PR 当作需求提报，就改成「是」；`triage` 读这个 flag。）*

改成「是」之后，PR 走**和 issue 一样的标签与状态**，用 `gh pr` 那套对应命令：

- **读 PR**：`gh pr view <number> --comments`，看 diff 用 `gh pr diff <number>`。
- **列出待 triage 的外部 PR**：
  `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`，
  然后**只留** `authorAssociation` 是 `CONTRIBUTOR`、`FIRST_TIME_CONTRIBUTOR` 或 `NONE` 的
  （丢掉 `OWNER`/`MEMBER`/`COLLABORATOR`）。
- **评论 / 打标签 / 关闭**：`gh pr comment`、`gh pr edit --add-label`/`--remove-label`、`gh pr close`。

**GitHub 的 issue 和 PR 共用一个编号空间**，所以光一个 `#42` 可能是两者之一：
先 `gh pr view 42`，不行再退回 `gh issue view 42`。

## 当某个 skill 说"发布到 issue tracker"时

建一个 GitHub issue。

## 当某个 skill 说"取回相关工单"时

跑 `gh issue view <number> --comments`。

## Wayfinding operations

供 `wayfinder` 使用。**地图**是单个 issue，**工单是它的子 issue**。

- **地图**：一个打了 `wayfinder:map` 标签的 issue，正文放 Notes / Decisions so far / 迷雾。
  `gh issue create --label wayfinder:map`。
- **子工单**：一个作为 GitHub sub-issue 链到地图上的 issue（对 sub-issues 端点跑 `gh api`）。
  没启用 sub-issues 时，把子工单加进地图正文的任务清单，并在子工单正文顶部写 `Part of #<map>`。
  标签：`wayfinder:<type>`（`research`/`prototype`/`grilling`/`task`）。
  **一旦被认领，这张工单就指派给驱动它的那位开发者。**
- **阻塞**：用 GitHub 的**原生 issue dependencies**，那是**权威的、UI 里看得见**的表示。
  加一条边：
  `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`，
  其中 `<blocker-db-id>` 是阻塞者的数字**数据库 id**
  （`gh api repos/<owner>/<repo>/issues/<n> --jq .id`，**不是** `#number`，也不是 `node_id`）。
  GitHub 会报 `issue_dependencies_summary.blocked_by`（只算打开着的阻塞者，那就是实时的闸）。
  没有 dependencies 可用时，退回到子工单正文顶部的一行 `Blocked by: #<n>, #<n>`。
  **所有阻塞者都关闭了，这张工单才算未被阻塞。**
- **前沿查询**：列出地图打开着的子工单（`gh issue list --state open`，范围限定在地图的
  sub-issues / 任务清单里），**去掉有打开着的阻塞者的**
  （`issue_dependencies_summary.blocked_by > 0`，或 `Blocked by` 那行里还有打开着的 issue）
  **和有 assignee 的**；**地图顺序里排第一的胜出。**
- **认领**：`gh issue edit <n> --add-assignee @me`，**本次会话的第一个写操作**。
- **解开**：`gh issue comment <n> --body "<答案>"`，然后 `gh issue close <n>`，
  再往地图的 Decisions so far 追加一个上下文指针（要点 + 链接）。
