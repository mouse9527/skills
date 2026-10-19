---
name: workspace
description: 工作区（ws）的骨架、创建、日常使用与收尾规则。所有 ws 放在 ~/workspaces/<名>/ 下，每项工作以一个 GitHub Issue 为锚点，本地 issues/<issue-id>/ 与 Issue、分支、PR 用同一个号对上。新建 ws、开始或收尾一个 Issue、拉或回收 worktree、合并 PR、整理 ws 文档时读本 skill。
---

# workspace

ws 是**工作台**，不是事实源。一个 ws 对应一条交付线，放在 `~/workspaces/<名>/`。
**每项工作都以一个 GitHub Issue 为锚点**：Issue 记目标和验收，PR 链回 Issue，本地目录按 Issue 号命名 ——
三者用同一个号对上，任何时候都能从 Issue 找到代码和工作区，反过来也一样。
本 skill 只讲通用骨架和纪律；某个 ws 的仓库、权限、部署写在它自己的 `README.md`。
下文路径都相对 ws 根目录。

## 1. 骨架

```
~/workspaces/<名>/
├── README.md              本 ws 的规则（唯一规则文件）
├── CLAUDE.md -> README.md 软链接；需要时 AGENTS.md 也链到它
├── NOW.md                 当前在做什么，便签，可丢弃，不进 git
├── .gitignore             至少 NOW.md、repos/、issues/
├── .templates/
│   └── issue.md           Issue body 模板，手工复制到 GitHub Issue
├── repos/<repo>/          repo 本体，停在基底分支，不在这里改代码
└── issues/<issue-id>/     一个 GitHub Issue 一个目录，号与 Issue 一致
    ├── <repo>/            该 Issue 的 worktree，只拉真正要改的 repo
    └── TASK.md            可选：派 worker 的任务书 / 交接，开头链回 Issue
```

可选，按需才建：
- `adr/` —— 架构决策暂存，最终提交进对应 repo 的 `docs/adr/`。
- `notes/<issue-id>/` —— Issue 关闭后要留的结论（评审记录、删掉的分支 SHA 等），不放过程证据。

`<issue-id>` 是 README 里约定的「Issue 所在 repo」的 Issue 号。**没有 Issue 就不建目录**：先开 Issue，再开工。

## 2. README.md 必须回答的问题

建 ws 时逐项填，不知道就写「待定」，不要猜：

1. **这是什么 ws**：一句话用途。
2. **Issue 开在哪个 repo**：一个 ws 只用一个 repo 的 Issue 编号，`issues/<issue-id>/` 的号以它为准。
3. **权威来源表**：每类事实只有一处，本地只读不复制。

   | 信息 | 权威来源 | 本地做什么 |
   |---|---|---|
   | 目标 / 验收标准 / 不做什么 / 止损 | GitHub Issue | 只读，`gh issue view <issue-id>` |
   | 候选版本 | PR + commit SHA | 只读 |
   | 验证结果 | PR checks / 运行环境实测 | 只读 |
   | 当前在做什么 | `NOW.md` | 随手改，丢了无所谓 |

4. **仓库表**：repo、remote、默认分支、PR 目标分支、CI 在哪、哪些只读。
5. **权限边界**：能部署到哪、谁授权、哪些环境 / 数据只读。
6. **本 ws 的验收判据**：什么才算 done（checks 绿？页面验收？运行态检查？）。
7. **本 ws 追加的规则**：只写和通用规则不同的地方，事故原因一句话。

README 超过约 300 行就体检：每段问「是纪律还是操作手册」，操作手册拆出去或删。

## 3. 一个 Issue 的流程

```
1. 开 GitHub Issue（按 .templates/issue.md），拿到 <issue-id>
2. issues/<issue-id>/ 下按需拉 worktree，分支名带上号：<type>/<issue-id>-<短描述>
3. 实现 → 各 repo 提 PR，body 写 `Part of <owner>/<repo>#<issue-id>`
4. 验收：按 README 的判据，Issue 里每条验收标准都有证据
5. 逐条打钩 → 合并 → 关 Issue（跨仓库的 Closes 不生效，手动关）→ 回收 issues/<issue-id>/
```

**号是唯一的连接**：Issue 号、目录名、分支名、PR body 四处一致。
看状态从 Issue + PR + checks 现算（`gh issue view` / `gh pr list --search <issue-id>` / `gh pr checks`），不在本地记。
范围变了先改 Issue，再改代码；Issue 里没有的不做。

`.templates/issue.md` 至少含四段：**验收标准**（每条写出能区分对错的验证方式）、**不做什么**、**止损条件**（触发就停下来问）、**done 的边界**。

## 4. worktree 生命周期

所有改动都在 worktree 里做，`repos/` 本体只停靠和派生。

```bash
# 开工：先确认基底（线上的 commit 在哪条线上）
git -C repos/<repo> fetch
git -C repos/<repo> branch -r --contains <线上 commit>
git -C repos/<repo> worktree add ../../issues/<issue-id>/<repo> -b <分支> origin/<基底>

# 回收：两条都为空才回收
git -C issues/<issue-id>/<repo> status --porcelain
git -C issues/<issue-id>/<repo> log --oneline @{u}..
git -C repos/<repo> worktree remove "$PWD/issues/<issue-id>/<repo>"
git -C repos/<repo> worktree prune
```

- 删 `issues/<issue-id>/` 前，先把要留的结论移到 `notes/<issue-id>/` 或 Issue 评论；不要连同唯一的报告一起删。
- 搬 ws：推分支后重新 clone，**不要 `mv`**（worktree 记的是绝对路径）。
- push 永远显式写 `refs/heads/<源>:refs/heads/<目标>`，杜绝误推默认分支。
- 多个 Issue 并行时禁用 `git stash` —— 它是仓库级的，会串到别的 worktree。

## 5. 合并纪律（交付类 ws）

两个条件同时满足才合并：

1. **PR checks 全绿。**「存量失败」「本地跑过了」「只改了文档」都不是豁免。`no checks reported` 是没跑，不是通过。
2. **源分支包含最新的目标分支**：`git merge-base --is-ancestor origin/<目标> <源>`。落后就在 PR 分支里 `git merge origin/<目标>`（用 merge 不用 rebase，冲突逐段解）。push 前、合并前各重读一次目标分支，不用早先记下的 SHA。

不在本地跑完整 CI 当门槛，让 PR 的 CI 去验；本地只跑和改动相关的部分用于定位。
并行的几个 PR 各自绿 ≠ 合起来绿：合并后看目标分支的 CI，并和合并前的失败集合逐条比对。

## 6. 验收判据

- **跨端改动两侧各自绿 ≠ 合起来对**：至少要有契约测试或端到端实跑之一；用可辨认的标记值、抓真实请求、留一个不该变的对照项。
- **验收覆盖 Issue 标题声称的全部范围**；调查发现的缺口要么成为验收项，要么写进「不做什么」。
- **测试绿 ≠ 用户能用**：有界面就做页面验收，有运行环境就看运行态。
- **断言守行为，不守写法**：不写扫源码文本的测试。
- **新加的规则先问是不是既有行为**，新要求找 Owner 确认再写。
- **归因靠对照实验**，不靠同时出现；排查两轮无结论就停止推理，去测量每个环节的实际状态。
- 部署后验实际部署的内容（镜像 tag、版本号），不以「workflow 成功」代替。

## 7. 角色

| 角色 | 负责 | 不负责 |
|---|---|---|
| **Owner**（用户） | 业务决策、范围变更、授权 | — |
| **lead**（主 agent） | 排期、派活、读 checks / 产物验收、识别止损 | 写产品代码、替 Owner 答业务问题 |
| **worker** | 一个 Issue 内的实现 | 静默扩大范围、未授权的分支操作（rebase / merge / reset / push） |

需要派 worker 时用 skill `herdr-workers`。汇报时事实标来源，推断标依据和不确定性。

## 8. 防膨胀

- **不为这套流程写工具**：不建 CLI、状态机、本地 backlog、证据库、跨 ws 注册表。Issue 状态从 Issue + PR + checks 现算，不落盘。
- 规则只写一处：通用的在 skill，ws 特有的在它的 README；不复制，只指向。
- 一条规则连续几个 Issue 都没帮你做出不同决定，删掉。
- 停掉 herdr / 结束 session / 换 agent CLI，任何一个让交付无法继续，说明耦合过强。

## 9. 新建 ws

1. 问清：ws 名、用途、Issue 开在哪个 repo、涉及哪些 repo 及 remote、能部署到哪。答不上来的写「待定」。
2. 建骨架：

   ```bash
   W=~/workspaces/<名>
   mkdir -p "$W"/{repos,issues,.templates}
   printf 'NOW.md\nrepos/\nissues/\n' > "$W/.gitignore"
   ln -s README.md "$W/CLAUDE.md"
   ```

3. 按第 2 节写 `README.md`，按第 3 节写 `.templates/issue.md`，建空的 `NOW.md`。
4. clone 只读还是可写的 repo 由用户决定；创建步骤本身不 clone、不拉 worktree、不起 agent。
