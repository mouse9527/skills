---
name: herdr-workers
description: 用 herdr 把任务交给另一个 agent（worker）时的布局、启动、派发、监控、验收、回收规则。凡是要 herdr agent start/prompt 一个 worker、整理 herdr 里 lead 与 worker 的 tab/pane、或核实 worker 交回的结果时，先读本 skill。委派子任务用 herdr worker，不用 Agent 工具子代理。
---

# herdr worker

本 skill 只管「怎么用 herdr 管另一个 agent」，与任务内容、项目结构无关。
worker 能写哪些文件、交付物怎么验、谁能授权什么，由调用方的规则决定，写进任务书。

术语：**lead** = 派活、读产物的 agent（通常是用户直接对话的那个）；**worker** = 被派活的 agent。

## 1. 布局：一个 lead 配一个 worker tab

目的：人一眼认出 lead，lead 的 tab 不被分屏，worker 也好找。

- **认 lead 看左侧 agents 侧栏**：worker 必有 name（`agent start <NAME>`），无 name、显示 kind（如 `claude`）的是 lead。
- **不在 lead 的 tab 里 split**（覆盖 herdr 内置 skill「默认 sibling pane」的建议）。
- **不改名、不关闭任何 lead 的 tab**（包括自己的）。认 lead 看侧栏，不看 tab 名 —— tab 名常常是过时的。
- lead 的所有 worker 放进同一个 tab `W·<lead 主线>`，**话题不同也放一起**，别每个话题开一个 tab。
  名字按 lead 实际在做的事起（`W·CI`、`W·issue-42`，不带 `#`），不照抄 lead tab 的名字。
- 复用 `W·` tab 前确认是自己建的或明确交接给自己的；前缀相同不代表归属。
- 一个 worker tab 最多 2×2 四个；超了开 `W·<话题>-2`。
- **cwd 只能在建 pane / tab 时定**，`agent start` / `prompt` 都改不了 → 每次显式传绝对 `--cwd`，不继承。

```bash
# 首个 worker：按 label 找自己的 worker tab（不记 tab ID，重启会变），没有就建
herdr tab list --workspace "$HERDR_WORKSPACE_ID"
herdr tab create --workspace "$HERDR_WORKSPACE_ID" --label 'W·<话题>' --cwd <绝对路径> --no-focus
#   → .result.root_pane.pane_id

# 后续 worker：第 2 个向右，第 3、4 个在左右两半里向下
herdr pane split --pane <root_pane> --direction right --cwd <绝对路径> --no-focus
herdr pane split --pane <左或右 pane> --direction down --cwd <绝对路径> --no-focus
#   → .result.pane.pane_id
# --direction 只接受 right / down；想放上方，就对上方那个 pane 用 down

# 整理旧布局（不重启 agent，name 跟随）
herdr pane move <pane> --new-tab --label 'W·<话题>' --no-focus
herdr pane move <pane> --tab <tab_id> --target-pane <pane> --split right --no-focus
```

坑：
- 源 tab 处于 zoom 时，`pane move` 返回成功但 `changed:false, reason:zoomed_tab`，什么都没动 → 先 `herdr pane zoom --pane <p> --off`。
- `pane move --new-tab` 用 `--label`；`--tab-label` 是 `--new-workspace` 才用的。
- worker 重启后可能丢 name，侧栏上会被误认成 lead → 立刻 `herdr agent rename <pane> <name>` 恢复。

## 2. 起 worker

```bash
herdr agent start <name> --kind claude --pane <pane> -- --model <档位>
```

- pane 必须停在 shell 提示符。
- **永远显式写 `-- --model`**，用用户定义的档位别名（如 `opus` / `sonnet` / `fable`）：省略参数会跟着默认值悄悄变。
  不同 CLI 的档位名各自确认，不互相照搬。需要固定型号做比较或排查时，才写完整模型 ID，并如实说明代理实际路由是否已确认。
- 选档：错了会静默坏数据/坏线上 → 最强档；方案已定只剩落地 → 中档；只读或产出可逐条核验 → 轻档。审查者不应比被审对象弱。
- **权限模式以实际为准**：启动后 `herdr agent read <name>` 看底栏 / 进程参数显示的有效模式。shell 别名、全局配置可能已经让 worker 跳过审批，不能从「我没传危险参数」推定它受限。
  模式与预期不符时先如实告诉用户。跳过审批只在用户明确授权的范围使用，并写进任务书；它不扩大任务授权。
  codex 受限启动：`-c sandbox_mode=workspace-write -c approval_policy=on-request`。
- 启动后看一眼屏幕：首次进目录或某些 CLI 会卡在信任 / hook 审核界面，第一条 prompt 会被吃掉。读清当前选项，选「不信任继续」类的那一项（编号随版本变，别背）；要不要信任交给用户。
- `agent start` 失败时 pane 里可能留着一个无名 agent —— 它会在侧栏伪装成 lead，关掉或命名。

**同一件事的后续任务继续派给原 worker**（它读过的上下文就是资产）；换一件事、换目录就新建 worker。
上下文撑爆要 `/clear` 前，先让它把结论写进文件。

## 3. 派发

任务书写成文件，prompt 只给路径 —— lead 断了，后继者也找得到。任务书至少写：

- 目标、范围、**不做什么**、止损条件
- **交付物是哪些文件**；分析类任务写完 `ls -la` 贴时间戳自证
- 能写哪些路径、哪些只读（由调用方规则给出）
- 「自己做，不要起子 agent / fork」—— worker 的 fork 会卡在 blocked，外面诊断不了
- 「小步落盘，每步简短报进度」—— 断线后知道做到哪；**调查类任务给落盘时限**（如「N 分钟内先写结论，拿不准的标待核」），否则会无限深挖
- 文字报告第一行写 `STATUS: in-progress | done | blocked（原因）`，随进度更新 —— 它是交接信号，不是完成判据
- 需要用户决定的问题写进交付文件交回，不自己定

```bash
herdr agent prompt <name> "读 <任务书绝对路径> 并照做。"   # 不加 --wait，否则阻塞后续派发
sleep 8; herdr agent get <name>                          # 必须变 working
```

`prompt` 返回成功不代表送达：名字丢了会静默丢弃；worker 可能处于「idle/ready 但不接活」（seq 会 +2，却不动）。
没转 working → `herdr agent list` 核对名字，`herdr agent read <name>` 看屏幕。

- 对 `working` 的 worker 追加 prompt 会排队到它这一轮结束；发完用 `agent read` 确认已在输入队列。追加内容长也写文件。
- 等待用 ≤10 秒的 `sleep`；更久的等待交给 `agent wait` 或监控（部分运行环境会拦截长 sleep）。

## 4. 监控：派发即建，两者成对

每次 `agent prompt` 后问一句：**现在有没有一个活着的监控覆盖这个 worker？** 没有就建。「上一个还在」不算数。

- 单个 worker：后台跑 `herdr agent wait <name> --until idle --until blocked --until done --timeout <ms>`，状态一变就返回，能抓到短暂的 blocked。
- 多个 worker：后台跑本 skill 目录下的 `watch_workers.py <name> [<name>...]`（只收名字，不收选项），任一状态变化或**消失**都打一行，全部停下后退出。
- 「没有事件」必须能区分「还在跑」和「已经没了」：agent 从列表消失要当终止事件。
- 监控脚本别用 macOS 自带 bash 写（3.2，没有 `declare -A`，开头就死而 stderr 不报）。起监控后读一次它的输出，确认活着。
- `working` 不代表还没产出：出结果了可能没转 idle，隔一段时间 `herdr agent read <name> --source recent-unwrapped` 看一眼。
- `blocked` = 在等输入（提问框、卡在 fork、权限审批）。先读清它在等什么再处理；该用户决定的转给用户。
- **卡在命令审批框**：读完整命令和实际目标。在任务书已授权范围内、效果可判断的，lead 可放行（含已授权的本地编辑与测试）；
  效果不清的让 worker 解释或拆小；越界的拒绝并告诉它原因；需要新授权的交给用户。不凭命令前缀或「看起来只读」放行。
- **监控会失效**：宿主工具的超时到期、lead 会话重启 / 恢复后，监控可能已经没了，这不代表 worker 结束。
  收到过期通知或刚恢复会话时，先 `herdr agent list` 看现场，再给仍在 working / blocked 的 worker 补监控。
- worker 没活时停掉对它的监控，否则空闲 worker 会被周期性重复报。

## 5. 验收：只信产物

`done` / `idle` ≠ 完成。状态区分不了「做完了」「断线了」「分析完没落盘」。

- 每次交回，按任务书列的交付物逐项去看：文件在不在、时间戳新不新、内容是否对得上。用调用方规定的方式查（例如看提交、看 PR）。
- 报告首行 `STATUS: blocked` 要处理，即使 herdr 显示 done；`STATUS: done` 也只是信号，仍按交付物验。状态在 done / working 之间抖动时，以本轮报告和产物为准。
- 产物为空 → `herdr agent read <name> --source recent-unwrapped`，找 `API Error` / `Connection error` / `ECONNRESET` / `Request timed out`。
- 断线：先把 worker 已做的改动保住（本地操作和网络操作分开跑，别让一个卡住拖住另一个），逐个核对断前最后几步有没有留下半截，
  再续派：「上一轮是断线不是你的问题，范围已定直接动手，小步落盘」。
- worker 在 pane 里报的数字，没落盘就等于不存在。
- worker 的附带结论要单独核实，主体对不等于每条都对；它给的命令参数用 `--help` 核过再用。
- 转述给用户时，事实标来源，推断标依据和不确定性。

## 6. 回收

- 某个 worker 的事已交付且无后续 → 关它的 pane（不是每次 prompt 返回 done 就关，那会丢掉可复用的上下文）。
- lead 结束 → 关它的 `W·` tab。
- 只关自己建的 pane / tab；别人的先问。lead 的 tab 永远不由别的 agent 关。
