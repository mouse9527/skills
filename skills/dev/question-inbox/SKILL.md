---
name: question-inbox
description: 问题收件箱：lead 需要 Owner 拍板或批准时，用 q.py ask（审批加 --kind approval）把问题写进本地收件箱，Owner 在 127.0.0.1:7788 的网页上回答，答案自动投回原 lead 的 herdr pane；同一页面也收 Owner 的一句话需求并投给 Router。要向 Owner 提问或请批准、收到 [question-inbox] 投递、要 receive / resolve / 撤回一题、或要查需求记录时读本 skill。
---

# 问题收件箱

收件箱**只放等 Owner 决定的问题**；Issue 进度、PR、CI 不放这里，Issue 仍是事实源。
**只有 lead 写收件箱**：worker 把要 Owner 决定的事写进交付物的 `## 需要 Owner 决定`，由 lead 分拣后再问。
**没答就没有授权**：空着、沉默、`blocking`、超时都不是同意；`需要更多上下文`、`暂不决定` 也不是。

```bash
Q="python3 $HOME/.claude/skills/question-inbox/q.py"   # 下文都用 $Q（双引号里 ~ 不展开，所以写 $HOME）
```

第一版只支持 herdr 里 `agent == claude` 的 lead。所有命令都要在 lead 自己的 pane 里执行（靠 `$HERDR_SESSION` / `$HERDR_PANE_ID` 核对身份）；
`receive`、`resolve`、`withdraw`、`erratum`、`ask --supersedes` 只有发题的原 lead 能做，别的会话执行一律退出码 4、什么都不改。
`~/workspaces/_questions/*.md` **只读**：不要用编辑器或 Edit 改，一切写入走 `$Q`。

## 1. 什么时候问

- 要 Owner 授权或拍板的事（范围、取舍、对外动作、花钱、删数据）→ `$Q ask`；聊天里最多带一行「待你回答：<id>」。
- 证据不够的先补证据再问；技术取舍也可以问，但要写清各方案的优缺点和你的推荐。
- 同一件事的几个子问题放一题（Q1…Qn）；不相关的事分开发。
- 事实变了：小修补用 `$Q erratum`（只在 Owner 还没答时可用），实质改动用 `--supersedes` 发新题替换旧题。

## 2. 怎么写一题

正文写进一个临时 md 文件，`$Q ask` 硬校验；不过就改，没有 `--force`。

```markdown
## 问题
Q1. 接口失败时要不要自动重试？
Q2. 重试上限定多少？

## 背景
不看聊天记录也能看懂：这条线在做什么、卡在哪里（≤ 12 行）。

## 证据
`src/client.py:88` 超时直接抛错；`make e2e` 失败 3/20 次；PR / Issue 链接。

## 选项
### Q1
- A. 自动重试（后果：……）
- B. 不重试，报错给调用方（后果：……）
### Q2
- A. 3 次
- B. 5 次

## 建议
Q1: A —— 理由
Q2: A —— 理由

## 答完之后
拿到答案后会做什么（软提示）。
```

- 每个 Qn 在 `## 选项`（`### Qn` 下的列表项就是网页上的选项）和 `## 建议`（`Qn: 建议 —— 理由`）里都要有。
- 不写「上一条消息 / 见上文 / 如上所述 / 同上 / 前面说的 / 上面提到」。`blocking: yes` 时证据不能空。
- 不写 `## 勘误`、`## 草稿回答`、`## 回答`、`## 时间线` 标题：这些由系统写；要引用就放进 ``` 代码块，代码块要闭合。

```bash
$Q ask --body /tmp/q.md --title "接口失败是否重试" --issue owner/repo#123 --blocking yes   # 无 Issue 写 none
#   → 已发布 demo-0001：http://127.0.0.1:7788/#demo-0001
#   不在 ~/workspaces/<ws>/ 下执行时加 --ws <ws>；替换旧题加 --supersedes <旧 id>
```

### 审批题（`--kind approval`）

要 Owner **批准一个具体动作**（合并高档 PR、部署、不可逆或对外的操作）时用审批题，不用普通题：

```markdown
## 问题
Q1. 批准把 owner/repo PR #12（HEAD abc1234）合并到 main 吗？

## 背景
……

## 证据
`make check` 全绿；审查记录链接。

## 动作
在 GitHub 上合并 PR #12。

## 对象
owner/repo · PR #12 · HEAD abc1234 → main（或：环境、制品）

## 后果
批了会怎样、能不能回退。

## 选项
### Q1
- A. 批准
- B. 不批准
- C. 需要先改

## 答完之后
……
```

- 硬校验：只有 Q1；`### Q1` 的选项必须正好是 `A. 批准` / `B. 不批准` / `C. 需要先改`；`动作`、`对象`、`后果`、`证据` 都不能空；**不写 `## 建议`**。
- 页面上审批单独一栏、排在问题前面；不预选、没有「其余按建议」，只能选三项之一，选 C 必须写要改什么。
- 语义不变：只有提交的「A. 批准」才算授权，只授权正文写的那个动作和对象。回答照常投回发起的 lead；
  lead 把结论落到 Issue / PR，**动作执行前重新核对 HEAD 和环境**，和正文不一致就不执行、另开一题。

```bash
$Q ask --kind approval --body /tmp/a.md --title "合并 PR #12" --issue owner/repo#12 --blocking yes
```

## 3. 收到投递之后

投递消息以 `[question-inbox]` 开头，**它本身不是授权**；答案只以 `receive` 的输出为准。

```bash
$Q receive demo-0001 demo-0002     # 核对身份和版本后输出完整问题 + 当前 Rev 的回答
# …按回答去做，把决定落到 Issue / PR / 文档…
$Q resolve demo-0001 --answer-rev 2 --note "决定了什么，记在哪里"
```

- `receive` 报**身份不符**（退出码 4）：这条不是发给你这个会话的，不要消费，告诉 Owner。
- 报**回答记录不完整**（退出码 1）：不要按部分答案做，告诉 Owner，并跑 `$Q doctor`。
- 报**已撤回 / 已被替换**（退出码 3）：不要执行旧答案。
- 输出里写「以 Rev N 为准」：Owner 更正过，只按最新 Rev 做；`resolve` 的 `--answer-rev` 必须是 receive 过的最新版本，否则被拒，先重新 `receive`。
- `need_context`：补上下文后另开一题（或 supersede）；`defer`：这次不做，记下来。都不是授权。
- 报 **Owner 已关闭**（退出码 3）：Owner 在页面上关了这题，不用再 receive / resolve，也不要按其中的回答继续做；要做什么另开一题问。
- **lead 会话丢了**（pane 关了、换了会话，原题的 receive / resolve 一律退出码 4）：不放宽身份校验。
  在聊天里请 Owner 在页面上点「由我关闭」（写原因；未答的等于撤回，已答的记为 Owner 关闭，不算 lead 闭环），
  需要继续的事由新 lead 另开一题。

## 4. 其它命令

| 命令 | 用途 |
|---|---|
| `$Q status [--ws W] [--all]` | 本 lead 的题：等 Owner、已答未领取、已领取未闭环 |
| `$Q show <id>` | 只读查看全文 |
| `$Q withdraw <id> --reason "…"` | 撤回；已投递过的会另发一条撤回通知 |
| `$Q erratum <id> --note "…" [--drop Q3]` | Owner 还没答时补勘误，可撤回个别子问题 |
| `$Q doctor` | 一致性检查（rev 回退、替换成环/悬空、停在 sending 等） |

## 4.1 需求记录、讨论线程与 Router（workspace §9 第二个例外）

Owner 在页面「需求」栏提交一句话 → `needs/need-NNNN.json` → 服务投给已登记的 Router（skill `router`）→ Router 判断归哪个 ws、交给 `lead-<ws>`
→ lead claim、需要时在需求的**讨论线程**里和 Owner 谈清楚 → 在本 ws 主 repo 建 Issue（workspace §3）。
需求记录只存 Owner 原话、分配结论（ws、why）、负责 lead 的身份、讨论线程、Issue URL（第一个是根）、notes 和投递记账；**不存**进度、stage、PR、谁在做（现算）。
文件只经过 `q.py` / 服务写入，对人只读。

状态：`new` →（Router take）`routing` →（route）`assigned` →（lead claim）
→ 清楚：`link` → `filed`；不清楚：`discussing`（`等 Owner` / `等 lead`）→ Owner 确认最新理解卡 → `confirmed` → `link` → `filed`；
不做：`dropped`（必须关联一个 not planned Issue）。另有 `held`（Router / lead 找不到路、卡住）。`filed` 后讨论线程**继续可用**（修订 1）；只有 `dropped` 封存、只读。

| 命令 | 谁 | 用途 |
|---|---|---|
| `$Q need list [--todo] [--status S] [--json]` / `$Q need show <id>…` | 任何人 | 只读（show 含讨论线程） |
| `$Q need take <id>…` / `$Q need route <id> --ws W --why "…"` / `$Q need note` | 只有登记的 Router | 领取 / 分到 ws / 记说明 |
| `$Q router dispatch <id>` / `$Q router lead <ws>` / `$Q router close-lead <ws>` | 只有 Router | 写任务书并交给 `lead-<ws>`；起 / 关 lead（只在 session router 里，只认 `router-leads.json` 里自己建的；暂停时不派、不新起；close-lead 要整个 workspace 里只有这个 lead 和它的 worker 才关） |
| `$Q need claim <id> [--takeover]` | 那个 ws 的 lead：pane 的 cwd **恰好**是 lead 目录、agent 名 `lead-<ws>`（worker 在子目录里，不能 claim） | 登记为负责 lead；takeover 只在原负责 lead **确认**不在时可用（查询失败、超时、多个匹配都算查不清，拒绝） |
| `$Q need card <id> --body <文件>` | 只有负责 lead | 发理解卡：`## 理解` / `## 范围` / `## 不做什么` / `## 验收草案` / `## 开放问题`（开放问题每条带推荐，可空）；每发一张版本 + 1 |
| `$Q need link <id> --issue <URL>…` / `$Q need drop <id> --issue <URL> --why "…"` | 只有负责 lead | 建好 Issue 后登记（讨论过的必须已确认；第一次 link 时核对根 Issue 在本 ws 主 repo、正文第一个非空行是 `需求：need-NNNN`；并自动贴一条「已确认的理解」评论，没有理解卡时贴「按原话立项（未单独讨论）」；水位回填到根 Issue 创建时间）/ 不做 |
| `$Q need inbox <id> [--ack <评论 id>…]` | 只有负责 lead | 列出未读的 Issue 评论（正文现场从 GitHub 取，不改状态）；`--ack` 才标已读 |
| `$Q need say <id> --text "…"` | 只有负责 lead | 在 7788 讨论线程里说一句（小进展，不进 GitHub、不投递） |
| `$Q need comment <id> --body <文件>` | 只有负责 lead | 把**结论**写到根 Issue（团队可见）；不贴讨论原文 |
| `$Q need hold <id> --why "…"` | Router（routing）或负责 lead（已立项不要 hold） | 卡住，页面显示原因 |
| `$Q router takeover <id>` | 只有 Router | 已立项需求的负责 lead **确认不在**：安排同 ws 的新 lead 接手（claim --takeover + need inbox） |
| `$Q router register` / `$Q router paused` / `$Q router status` | Router / 任何人 | 登记身份；暂停开关；登记、拉起次数、最近投递 |

身份不符一律退出码 4，状态不允许退出码 3。上面这些 Router / 负责 lead 的命令，身份**只取执行者自己 pane 的** `$HERDR_SESSION` / `$HERDR_PANE_ID`：
环境变量缺失、或 `--herdr-session` / `--pane` 和它们不一致，都是退出码 4、什么都不改。
（`ask` / `receive` / `resolve` / `withdraw` / `erratum` 按收件箱原规格仍可用这两个参数显式指定，因为它们会和发题时记下的 lead 会话核对；新增的特权命令没有这层，所以不开放。）

**lead 目录**：默认 `~/workspaces/<ws>`；tasks.json 的条目可以加可选字段 `"lead_cwd": "<绝对路径或 ~/…>"`（例如 ws 本身就是线上代码目录时）。
Router 起 lead、写派工书（`<lead 目录>/dispatch/`）、claim 校验都按它。
**`lead_cwd` 只能指向 Owner 自己的代码**：Router 起 lead 时会对这个目录自动选「信任」（herdr-workers §2：外部内容不自动信任）。

- **修订 1：讨论默认留在 7788，结论才进 Issue。** Owner 在详情页发的消息默认只进讨论线程、投给负责 lead；
  输入框旁的「同时发到 Issue（团队可见）」默认关，打开后才走 GitHub 评论路径（发送意图、comment id 记账、对账），
  回执显示「已发到 GitHub → 已通知 lead → lead 已读」。时间线来源分「7788 讨论」/「Issue 评论 · 经 7788」/「Issue 评论」。
- lead 判断什么时候把**结论**写进 Issue：范围 / 验收 / 取舍 / 优先级变化，或 Owner 明确说「记一下」时，用 `need comment`；
  小进展用 `need say` 留在 7788。Issue 评论是协作输入，不是授权。
- **Owner 在页面上**：对还没有负责 lead 的需求写的是「补充 / 纠正」（记成 note，Router 正在处理时投给 Router）；有负责 lead 之后写的是「回复」，
  进讨论线程并投给负责 lead；「确认理解卡 vN」只能确认**最新**一张（绑定版本，过期 409），确认后投给负责 lead 去建 Issue。
  等 Owner 回复或确认的需求出现在「待处理」最上面的「需求讨论」一栏。
- 还没 lead claim 的需求（assigned）：Owner 的补充投给 Router；分派后 30 分钟（`QI_UNCLAIMED_AFTER` 秒）仍没人 claim，服务自动补投 Router 一次，页面也给「重新通知」。
- 投递和回答投递同一套机制：按 agent_session 定位、写前记账、held / uncertain、退避、不重复发，同一批最多合并 5 条。
  投给负责 lead 时找不到它（pane 关了、换了会话）只暂缓，不改投别的 lead、也不拉起谁（由 Router 的看门狗处理）。
- **Router 不在时自动拉起**（没登记、或登记的 pane 没了 / agent 已退出）：服务在 session router 里建 workspace `Router`（cwd `~/workspaces`）、
  `agent start router --kind claude -- --dangerously-skip-permissions --model opus`，信任弹窗读屏后选「信任」，再发「读 skill router、register、处理待办」。
  session 的 socket 不在就只报暂缓、不创建；同一时间只拉一次，1 小时最多 3 次，超过页面标红「Router 起不来」并停止自动拉起（页面可重试，Router 登记后自动解除）。
  在不在用三态判断：**确认不在**才拉起；查询失败、超时、多个匹配、**输出结构不对**（缺列表字段、null、type 不对、条目缺 pane_id）算**查不清**，只暂缓。
  只有成功解析出的空列表才代表「确实没有」。
  session 里已有名为 router 的 agent、但会话不是登记的那个（会话重开过）：不重启它，只给它发一句「重新登记」，同一会话 10 分钟最多一次；页面有「让 Router 重新登记」按钮。
  拉起前，上一次建的 Router workspace 若只剩没有 agent 的空壳就关掉。绝不在别的 session 起 agent。
- **全局暂停**（页面开关）：不投给 Router、不拉起 Router，Router 不 take、不 route、不派工、不新起 lead。
  已在跑的 lead 不受影响：Owner 在讨论线程里的回复 / 确认照常投给负责 lead。
  暂停在 Router 侧每一次外部写动作（建 workspace、起 agent、按键、prompt、关 workspace）之前都重新检查，且和这些动作互斥：
  暂停提交之后不会再有新的启动 / 投递 / 派工；途中被暂停的投递退回排队，恢复后发一次。

## 5. 服务（网页 + 投递）

`server.py` 是常驻服务：绑定 `127.0.0.1:7788`，每 5 秒扫一次已答的题，找到 `agent_session` 与发题时相同的那个 claude pane 才发
（`herdr agent prompt`，不加 `--wait`）；找不到、blocked 就暂缓并退避重试，超时或结果不明就标「不确定」，等 Owner 在网页上点「重新通知」（回答、更正和撤回通知都有这个按钮）。
投给 lead 时服务从不 `agent start`、`--resume`、send-keys，也不改投别的 lead；唯一的例外是上面「Router 不在时自动拉起」，只在 session router 里、只对自己建的 workspace。

安装（用户级 LaunchAgent，一次性，由 Owner 或 lead 手动执行；未在 CI 验证）：

```bash
D="$HOME/.claude/skills/question-inbox"
mkdir -p "$HOME/.local/state/question-inbox"
sed -e "s#__PYTHON__#$(command -v python3)#" -e "s#__SERVER__#$D/server.py#" \
    -e "s#__HERDR__#$(command -v herdr)#" -e "s#__GH__#$(command -v gh)#" -e "s#__HOME__#$HOME#g" \
    "$D/local.question-inbox.plist.template" > "$HOME/Library/LaunchAgents/local.question-inbox.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.question-inbox.plist"
```

日志、token 在 `~/.local/state/question-inbox/`；日志只记 id、状态和错误摘要。
已经装过的：重新跑上面的 sed 生成 plist（多了 `GH_BIN_PATH`），再 `launchctl bootout` / `bootstrap` 一次，任务视图才能在 launchd 下找到 gh。

## 6. 页面

页面照线框：最左图标栏（待处理 / 需求），右侧是需求 / 待处理 / 工作区筛选和数量，底部「全部 Issue（只读视图）」。**需求**看板六列从左到右：待处理 / 卡住 / 待立项 / 审查中 / 进行中 / 完成（判断优先级仍是完成优先，显示顺序另排）；**待处理**只放要 Owner 动手的事，按需求合并；`#tasks` 是只读 Issue 视图。旧地址 `#needs` 跳到看板、`#needs:<id>` 跳到对应详情页。
详情页时间线里的问题 / 审批卡**就地作答**（沿用版本绑定、409、提交中禁用、提交后只读、多问摘要、迟到回包保护），复杂题可点「在完整页打开」。
需求页和答题页的写请求都过同样的 JSON、Host、Origin、token 四项校验；提交需求带客户端生成的 `request_id`，重复提交返回原记录。

## 6.1 任务视图（只读）

同一个页面顶部切到「任务」（`http://127.0.0.1:7788/#tasks`），或者在终端：

```bash
$Q tasks [--ws W] [--json]     # 和网页同一份数据；lead 回答「在做什么」用它，不用 NOW.md
```

**它是只读的，状态以 GitHub 为准。** 页面上除了刷新、链接、跳到题，没有任何输入控件；不写收件箱以外的任何东西，也不写 GitHub。
要改状态就去改 Issue / PR（关 Issue、加 label、合 PR），这里下次刷新就跟着变。

数据从哪来（每次现查，只在内存里缓存 60 秒，页面显示「数据时间」；「刷新」按钮只是带 `?refresh=1` 的 GET）：

- **repo 清单**：配置文件 `~/.config/question-inbox/tasks.json`（`QI_TASKS_CONFIG` 可覆盖），格式 `{"repos":[{"ws":"demo","repo":"owner/repo"}]}`。不从 README 猜。
- **GitHub**（`gh api graphql`，路径取 `GH_BIN_PATH`，默认 `command -v gh`）：每个 repo 最多 100 个 open Issue + 7 天内关闭的；Issue 的 labels、parent、子 Issue 计数；
  关联 PR 先用 Issue 侧的原生关联（linked / closing references），再补 PR 侧的 closing references 和正文 `Part of <owner>/<repo>#<号>`
  （靠 Issue 上的交叉引用找到这些 PR；单纯提到不算关联）；PR 的状态和 checks 汇总。任何一处截断（open Issue、关联 PR、交叉引用）都会提示「还有更多」。
- **herdr**：每个有 socket 的 session 跑 `pane list`（socket 不在就跳过，不创建 session）。只看有 agent 的 pane（普通 shell 不算）：
  agent 名是 `w-<号>…` / `rv-<号>-…`，或 cwd 在 `~/workspaces/<ws>/(issues|stories)/<号>/` 下，就算这个任务有人在做；页面写明依据是名字还是 cwd。
  缓存和失败**以 session 为单位**：某个 session 查询失败时，它上次的 agent 照用并标出时间，其他 session 照常刷新；从没查成功过的 session 让「待开工」写成「agent 情况待核」。
- **收件箱**：题头 `issue` 字段对上的未答题挂在任务上，点「去回答」跳到题；`issue=none` 或对不上的放「未关联的待答题」。

查询失败时（每个 repo、每个 herdr session 单独算）页面显示「无法刷新：原因」，保留那个来源上一次成功的数据并标出时间；从没成功过就写「没有可用数据（不代表没有任务）」。

stage（读的时候现算，每个都带依据；同时命中几个时显示最需要处理的那个，其余作小标签）：

| stage | 判据 |
|---|---|
| 等你回答 | 有挂在这个任务上的未答题 |
| 待合并检查 | 有 open、非草稿的 PR，checks 汇总**明确是 SUCCESS**（没有 checks、还在跑、草稿都不算，归「在做」并写明）；依据写「checks 全绿，审查和授权需在 PR 上核对」——它不代表可以直接合 |
| 等你验收 | 没有 open PR，但有已合并的 PR，Issue 还开着（例如合了还没上 prod 验收） |
| 卡住 | 对应的 agent 是 blocked，或 open PR 有失败的 check |
| 在做 | 有对应的 agent；或 72 小时内 Issue / PR 有更新；或有 open PR 还没有可合并的检查结果（草稿 / 尚无检查结果 / 运行中）。写「谁 · 多久前动过」 |
| 停着 | Issue 带 `blocked` / `on-hold` 这类 label（有 agent 时只作小标签） |
| 待开工 | Issue open，没有 agent、没有 PR、没有上面这些 |
| 最近关闭 | 7 天内关闭，「已完成」和「不做」分开标 |

- 按需求归组：根 Issue 正文第一行是 `需求：need-NNNN` 的，它和它的子 Issue 归到这个需求下（页面「按需求」一节；`#tasks:need-NNNN` 只看这个需求）。
- 分组按「要不要你动手」：等你（回答 / 待合并检查 / 验收）、卡住、在做、待开工、停着、最近关闭；空组不显示，组内按 ws 聚合。
- 父 Issue 显示子 Issue 进度（x/y 已关），**子 Issue 全关不等于父 Issue 完成**；只有 Issue 自己 closed 才显示完成。
  子 Issue 缩进在父 Issue 下面；父 Issue 在别的组时，先放一行「父 #N（在某组）」再缩进列出子 Issue。
- 未验证 / 已知盲区：本机 `pane list` 的输出里还没见过 agent 名字段，代码读 `name` / `agent_name` / `label`，按名字对应**未实测**，目前实际靠 cwd；只认 `issues/`、`stories/` 两种目录；
  配置里没有的 repo 里的 PR 只能靠 `Part of` 写全 `<owner>/<repo>#<号>` 才对得上。
