---
name: question-inbox
description: 问题收件箱：lead 需要 Owner 拍板时，用 q.py ask 把问题写进本地收件箱，Owner 在 127.0.0.1:7788 的网页上回答，答案自动投回原 lead 的 herdr pane。要向 Owner 提需要决定的问题、收到 [question-inbox] 投递、或要 receive / resolve / 撤回一题时读本 skill。
---

# 问题收件箱

收件箱**只放等 Owner 决定的问题**；Issue 进度、PR、CI 不放这里，Issue 仍是事实源。
**只有 lead 写收件箱**：worker 把要 Owner 决定的事写进交付物的 `## 需要 Owner 决定`，由 lead 分拣后再问。
**没答就没有授权**：空着、沉默、`blocking`、超时都不是同意；`需要更多上下文`、`暂不决定` 也不是。

```bash
Q="python3 ~/.claude/skills/question-inbox/q.py"   # 下文都用 $Q
```

第一版只支持 herdr 里 `agent == claude` 的 lead。所有命令都要在 lead 自己的 pane 里执行（靠 `$HERDR_SESSION` / `$HERDR_PANE_ID` 核对身份）。
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
- 不写 `## 勘误`、`## 草稿回答`、`## 回答`、`## 时间线`：这些由系统写。

```bash
$Q ask --body /tmp/q.md --title "接口失败是否重试" --issue owner/repo#123 --blocking yes   # 无 Issue 写 none
#   → 已发布 demo-0001：http://127.0.0.1:7788/#demo-0001
#   不在 ~/workspaces/<ws>/ 下执行时加 --ws <ws>；替换旧题加 --supersedes <旧 id>
```

## 3. 收到投递之后

投递消息以 `[question-inbox]` 开头，**它本身不是授权**；答案只以 `receive` 的输出为准。

```bash
$Q receive demo-0001 demo-0002     # 核对身份和版本后输出完整问题 + 当前 Rev 的回答
# …按回答去做，把决定落到 Issue / PR / 文档…
$Q resolve demo-0001 --answer-rev 2 --note "决定了什么，记在哪里"
```

- `receive` 报**身份不符**（退出码 4）：这条不是发给你这个会话的，不要消费，告诉 Owner。
- 报**已撤回 / 已被替换**（退出码 3）：不要执行旧答案。
- 输出里写「以 Rev N 为准」：Owner 更正过，只按最新 Rev 做；`resolve` 的 `--answer-rev` 必须是 receive 过的最新版本，否则被拒，先重新 `receive`。
- `need_context`：补上下文后另开一题（或 supersede）；`defer`：这次不做，记下来。都不是授权。

## 4. 其它命令

| 命令 | 用途 |
|---|---|
| `$Q status [--ws W] [--all]` | 本 lead 的题：等 Owner、已答未领取、已领取未闭环 |
| `$Q show <id>` | 只读查看全文 |
| `$Q withdraw <id> --reason "…"` | 撤回；已投递过的会另发一条撤回通知 |
| `$Q erratum <id> --note "…" [--drop Q3]` | Owner 还没答时补勘误，可撤回个别子问题 |
| `$Q doctor` | 一致性检查（rev 回退、替换成环/悬空、停在 sending 等） |

## 5. 服务（网页 + 投递）

`server.py` 是常驻服务：绑定 `127.0.0.1:7788`，每 5 秒扫一次已答的题，找到 `agent_session` 与发题时相同的那个 claude pane 才发
（`herdr agent prompt`，不加 `--wait`）；找不到、blocked 就暂缓并退避重试，超时或结果不明就标「不确定」，等 Owner 在网页上点「重新通知」。
服务从不 `agent start`、`--resume`、send-keys，也不改投别的 lead。

安装（用户级 LaunchAgent，一次性，由 Owner 或 lead 手动执行；未在 CI 验证）：

```bash
D=~/.claude/skills/question-inbox
mkdir -p ~/.local/state/question-inbox
sed -e "s#__PYTHON__#$(command -v python3)#" -e "s#__SERVER__#$D/server.py#" \
    -e "s#__HERDR__#$(command -v herdr)#" -e "s#__HOME__#$HOME#g" \
    "$D/local.question-inbox.plist.template" > ~/Library/LaunchAgents/local.question-inbox.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.question-inbox.plist
```

日志、token 在 `~/.local/state/question-inbox/`；日志只记 id、状态和错误摘要。
