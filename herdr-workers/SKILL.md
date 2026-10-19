---
name: herdr-workers
description: 用 herdr 派发、布局、监控、验收 worker agent 的现行规则。凡是要把调研/实现/评审交给另一个 agent（herdr agent start/prompt）、整理 herdr 里 lead 与 worker 的 tab/pane、或核实 worker 交回的结果时，先读本 skill。委派子任务一律用 herdr worker，不用 Agent 工具子代理。
---

# herdr worker 调度

现行规则只在这里改一处。各项目 / workspace 的 README 只写本地差异（可写边界、权限、部署环境）并指向本 skill。
历史事故档案可另存（例如某个 ws 的 `WORKER.md`），只作证据；与本文冲突以本文为准。

## 0. 角色与边界

- **lead**：协调、派活、读产物、验收；不写产品代码，不替 Owner 答业务问题。
- **worker**：在一个领域 + 一个 worktree 内实现；不静默扩大范围。
- 分支操作（rebase / merge / reset / push）须经 lead 授权，普通 commit 不用。
- 只用 herdr worker，**不用 Agent 工具子代理**（Owner 看不到、接不上手）。
- worker 卡住时 lead 也不亲自改代码兜底；重派或换 worker。

## 1. 布局：一个 lead 配一个 worker tab

目的：Owner 一眼认出 lead，lead 的 tab 不被分屏，worker 也好找。

- **认 lead 看左侧 agents 侧栏**：worker 必有 name（`agent start <NAME>`），无 name、显示 `claude` 的是 lead。
  例外：worker 重启后可能丢 name，会被误认成 lead —— 重启后立刻 `herdr agent rename <pane> <name>` 恢复。
- **不在 lead 的 tab 里 split**（这覆盖 herdr 内置 skill「默认 sibling pane」的建议）。
- lead 的所有 worker 放进同一个 tab `W·<lead 话题>`。话题按实际在做的事起（`W·CI`、`W·issue-42`），不照抄 lead tab 的 `1`/`3`。
- 一个 worker tab 最多 2×2 四个；超了开 `W·<话题>-2`。
- **cwd 只能在建 pane / tab 时定**，`agent start` / `prompt` 都改不了 → 每次显式传绝对 `--cwd`，不继承。
- 回收：某 worker 的领域已交付且无后续 → 关它的 pane（不是每次 prompt 返回 done 就关）；lead 结束 → 关它的 `W·` tab。

```bash
# 首个 worker：先按 label 找自己的 worker tab（不记 tab ID，重启会变）
herdr tab list --workspace "$HERDR_WORKSPACE_ID"
herdr tab create --workspace "$HERDR_WORKSPACE_ID" --label 'W·<话题>' --cwd <worktree绝对路径> --no-focus
#   → .result.root_pane.pane_id
herdr agent start <name> --kind claude --pane <root_pane> -- --model <档位>

# 后续 worker：第 2 个向右，第 3、4 个在左右两半里向下
herdr pane split --pane <root_pane> --direction right --cwd <另一 worktree> --no-focus
herdr pane split --pane <左/右 pane> --direction down --cwd <...> --no-focus
#   → .result.pane.pane_id，再 agent start

# 整理旧布局（不重启 agent，name 跟随）
herdr pane move <pane> --new-tab --label 'W·<话题>' --no-focus
herdr pane move <pane> --tab <tab_id> --target-pane <pane> --split right --no-focus
```

坑：
- 源 tab 处于 zoom 时，`pane move` 返回成功但 `changed:false, reason:zoomed_tab`，什么都没动 → 先 `herdr pane zoom --pane <p> --off`。
- `pane move --new-tab` 用 `--label`，不是 `--tab-label`（那是 `--new-workspace` 用的）。
- 新 pane 必须停在 shell 提示符才能 `agent start`。首次进新目录 / codex 启动可能卡在信任或 hook 审核界面，
  `agent read` 看一眼，必要时 `pane send-keys <p> Escape`；信不信任交给 Owner。
- `agent start` 失败时 pane 里可能留着一个无名的 claude —— 它会在侧栏伪装成 lead。

## 2. 选档与起 worker

| 档 | 起法 | 适用 |
|---|---|---|
| 重 | `--kind claude -- --model opus` | 跨端契约、事务/并发正确性、架构裁决、独立审查、复杂根因 |
| 中 | `--kind claude -- --model sonnet`，或 codex（`-c sandbox_mode=workspace-write -c approval_policy=on-request`） | 方案已定的多文件实现、测试补齐 |
| 轻 | `--kind claude -- --model fable` | 只读调查、采证、机械清理 |

- **永远显式写 `-- --model`**：默认档位由 Owner 控制，会变。
- 按「错了会不会静默坏数据/线上」→ 重；「方案已定只剩落地」→ 中；「只读或可逐条核验」→ 轻。
- 审查者不能是作者，也不应比被审对象弱。
- 不开全局 yolo（`--dangerously-bypass-approvals-and-sandbox`）——它会放行 push 和 worktree 外写入。

**worker 绑定「领域 + worktree」，不绑 Issue。** 同领域后续任务继续派给原 worker（上下文就是资产）；
换领域新建 worker；不让 worker 静默换 cwd。上下文撑爆时先让它把结论写进 `PROGRESS.md` 再 `/clear`。
多个 worker 同仓库时，在任务书里声明各自的文件边界；并行时禁用 `git stash`（它是仓库级的）。

## 3. 派发

任务书写成文件（`stories/<号>/TASK.md`、`tasks/<id>/TASK.md`），prompt 只给路径 —— session 断了后继者也找得到。任务书必含：

- 目标、范围、**本次不做什么**、交付物（**文件路径**）、止损条件
- 本 ws 的只读/可写边界、不许写 worktree 外文件
- 「自己做，不要起子 agent / fork」（worker 起 fork 会卡在 blocked，外面诊断不了）
- 「小步提交 / 落盘，每步简短报进度」（断线后知道做到哪）
- 「写完 `ls -la` 贴时间戳自证」（分析类任务）
- 业务问题写进文件交回 lead，不要自己定

```bash
herdr agent prompt <name> "读 <TASK.md 绝对路径> 并照做。"     # 不加 --wait，否则阻塞后续派发
sleep 8; herdr agent get <name>                                # 必须变 working
```

`prompt` 返回成功不代表送达：名字丢了会静默丢弃；worker 可能「idle/ready 但不接活」（seq +2 却不动）。
没转 working → `herdr agent list` 核对名字、`agent read` 看屏幕。

## 4. 监控：派发即建，两者成对

每次 `agent prompt` 之后问一句：**现在有没有一个活着的监控覆盖这个 worker？** 没有就建。「上一个还在」不算数。

- 单条线：`herdr agent wait <name> --until idle --until blocked --until done --timeout <ms>`（后台跑，状态一变就返回，能抓到短暂的 blocked）。
- 多条线：`python3 <本 skill 目录>/watch_workers.py <name>...`（Monitor 工具跑；任一状态变化或**消失**都报一行，全部停下后退出）。
- 监控必须能区分「还在跑」和「已经没了」：agent 从列表消失要当终止事件。
- 监控脚本用 Python，别用 bash —— macOS `/bin/bash` 3.2 没有 `declare -A`，脚本开头就死，而 stderr 不报。起监控后读一次它的输出，确认活着。
- worker 状态 `working` 不代表还没产出：出结果了可能没转 idle，隔一段时间 `herdr agent read <name> --source recent-unwrapped` 看一眼。
- `blocked` = 在等输入（常见：它开了提问框、卡在 fork、权限审批）。先读清它在等什么再处理；业务问题转 Owner。
- worker 没活时停掉监控，否则空闲 worker 会周期性重复报。

## 5. 验收：只信产物

`done` / `idle` ≠ 完成。状态区分不了「做完了」「断线了」「分析完没落盘」。每次交回都查：

```bash
git -C <worktree> status --porcelain        # 有没有改动
git -C <worktree> log --oneline <base>..HEAD # 有没有提交
gh pr list --state open --search "<号>"     # 有没有 PR（如该它开）
ls -la <交付文件>                           # 文件在不在、时间戳新不新
```

- 产物为空 → `herdr agent read <name> --source recent-unwrapped`，找 `API Error` / `Connection error` / `ECONNRESET` / `Request timed out`。
- 断线处理：先把工作区改动 WIP 提交保住（本地，不 push；**不要和 `git fetch` 写在同一条命令**），
  `git diff HEAD` 逐文件核一遍（断前最后几步常留半截），再续派：「上一轮是断线不是你的问题，范围已定直接动手，小步提交」。
- worker 在 pane 里报的数字，没落盘就等于不存在 —— 不要拿去向 Owner 汇报。
- worker 的附带结论要单独核实，主体对不等于每条都对；它给的命令参数也要用 `--help` 核。
- 跨端改动两侧各自绿 ≠ 合起来对；验收按本 ws README 的判据走。

## 6. 汇报

事实标来源（命令输出、文件:行号、PR 号），推断标依据和不确定性。worker 的输出转述给 Owner 前先按第 5 节核过。
