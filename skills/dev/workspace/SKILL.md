---
name: workspace
description: 工作区（ws）的骨架、创建、日常使用与收尾规则。所有 ws 放在 ~/workspaces/<名>/ 下，每项工作以一个 GitHub Issue 为锚点，本地 issues/<issue-id>/ 与 Issue、分支、PR 用同一个号对上。新建 ws、开始或收尾一个 Issue、拉或回收 worktree、审查或合并 PR、整理 ws 文档时读本 skill。
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
    ├── TASK.md            可选：派 worker 的任务书 / 交接，开头链回 Issue
    └── review/            审查任务书与审查报告 R<轮>-<审查者>.md（见第 5 节）
```

`REVIEW.md` 只指被审 repo 根目录的审查规则（随 repo 走 PR）；ws 里的审查产出不叫这个名字。

可选，按需才建：
- `adr/` —— 架构决策暂存，最终提交进对应 repo 的 `docs/adr/`。
- `notes/<issue-id>/` —— Issue 关闭后要留的结论（评审记录、删掉的分支 SHA 等），不放过程证据。

`<issue-id>` 是 README 里约定的「Issue 所在 repo」的 Issue 号。**没有 Issue 就不建目录**：先开 Issue，再开工。

**迁移期**：已有 ws 的 README 若仍用别的目录名（如 `stories/`），在迁移完成前以 README 为准，「号一致」的规则不变；
新建 ws 用 `issues/`。迁移由 Owner 安排，不在普通任务里顺手改名。

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
8. **审查**：各 repo 有没有根目录 `REVIEW.md`；风险表里和第 5 节默认不同的地方（本 ws 哪些路径算高档）。没有差异就写「按 skill 默认」。

README 超过约 300 行就体检：每段问「是纪律还是操作手册」，操作手册拆出去或删。

## 3. 一个 Issue 的流程

```
1. 开 GitHub Issue（按 .templates/issue.md），拿到 <issue-id>
2. issues/<issue-id>/ 下按需拉 worktree，分支名带上号：<type>/<issue-id>-<短描述>
3. 实现 → 各 repo 提 PR，body 写 `Part of <owner>/<repo>#<issue-id>`
4. 审查：本地 agent 审每个 PR，lead 核实、处置，记到 PR 上（第 5 节）；和 CI 并行
5. 验收：按 README 的判据，Issue 里每条验收标准都有证据
6. 逐条打钩 → 合并 → 关 Issue（跨仓库的 Closes 不生效，手动关）→ 回收 issues/<issue-id>/
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

# 回收：逐条执行并看结果，任何一条报错就停下
git -C issues/<issue-id>/<repo> fetch
git -C issues/<issue-id>/<repo> status --porcelain --ignored   # 应无需要保留的内容
git -C issues/<issue-id>/<repo> log --oneline @{u}..           # 应为空，且命令本身成功
git -C repos/<repo> worktree remove "$PWD/issues/<issue-id>/<repo>"
git -C repos/<repo> worktree prune
```

- **检查失败不等于检查通过**：没有上游、引用不存在、命令报错时，空输出不代表「没有未推送提交」，别用 `[ -z "$(… 2>/dev/null)" ] && remove`；默认不用 `--force`，核清剩下的都可丢弃才用。

- 删 `issues/<issue-id>/` 前，先把要留的结论移到 `notes/<issue-id>/` 或 Issue 评论；不要连同唯一的报告一起删。
- 搬 ws：推分支后重新 clone，**不要 `mv`**（worktree 记的是绝对路径）。
- push 永远显式写 `refs/heads/<源>:refs/heads/<目标>`，杜绝误推默认分支。
- 多个 Issue 并行时禁用 `git stash` —— 它是仓库级的，会串到别的 worktree。

## 5. 审查

每个 PR 合并前都由**本地 agent** 审查：lead 派审查 worker（起法见 `herdr-workers`「审查 worker」），收报告、核实、处置，结论记到 PR 上。
不依赖托管平台上的审查机器人。CI 回答「能不能跑」，审查回答「这样改对不对、会弄坏什么」，两者不互相代替。

**审查依据**，按这个顺序读：Issue（目标、验收、不做什么）→ 被审 repo 根目录的 `REVIEW.md`（这个 repo 什么最要紧、跳过什么；没有就只按 Issue 和下面的通用标准）→ repo 的 `AGENTS.md` / `CLAUDE.md` → diff。

**分档看改动，不看作者。** ws README 第 8 项写了就按它，没写按默认：

| 档 | 典型改动 | 审查 | 合并前还要 |
|---|---|---|---|
| 低 | 只改文档、注释、测试 | 1 个审查 worker | — |
| 中（默认） | 其余改动 | 1 个审查 worker，kind 尽量和实现者不同 | — |
| 高 | 数据库迁移与存量数据改写、认证鉴权、CI / 部署 workflow、网关路由与环境配置、删除接口或数据、README 列出的敏感路径 | 2 个不同 kind 的审查 worker 各自独立审 | Owner 看过审查记录，明确同意合并这个 HEAD |

拿不准就高一档；一个 PR 跨几档按最高的。diff 超过约 1000 行，先问能不能拆。

**一轮怎么走**

1. PR 开好后（CI 同时在跑），记下 `base`（`git merge-base origin/<目标> <head>`）和被审 `head` 的完整 SHA，
   照本 skill 目录下的 `review-task.md` 写任务书到 `issues/<issue-id>/review/`，派审查 worker。
2. 审查者交回 `review/R<轮>-<审查者>.md`：首行 `STATUS`，第二行
   `head <SHA>：<N> Important / <M> Nit / <K> Pre-existing` 或 `head <SHA>：没有需要修复的问题`。
3. **lead 逐条核实 Important**：打开 `文件:行号`，确认失败场景成立，必要时跑一条命令。
   说不出具体输入和错误结果的、纯风格的，降为 Nit 或丢弃，并写一句原因。高档两份报告分别核实，两份都提到的优先看。
4. **每条 Important 只有两个出口**：修（交回实现 worker，**同一类问题改全部实例**，不只改被点到的那一行），
   或写理由关闭（误报 / 有意如此 / 超出 Issue 范围，后者写进 Issue「不做什么」或另开 Issue）。
   Nit 不阻塞，lead 挑值得的给实现者。Pre-existing 不是本 PR 引入的，不算在本 PR 头上，值得修就另开 Issue。
5. **记到 PR**：lead 用 `gh pr comment` 发一条审查记录，写明被审 head SHA、档位、审查者 kind / 模型、
   每条 Important 的处置（修复的 commit，或关闭理由）。PR 上的这条记录是事实源，`review/` 里的文件只是过程。

**轮次与收敛**

- 修完后的下一轮**只审增量** `<上轮 head>..<新 head>`，并核对上轮的 Important 是否真修了；已处置的不重审，已关闭的不再提。
- **最多 3 轮。** 第 3 轮后还有新的 Important，就停止改审循环：lead 带着三轮记录重新考虑方案（拆 PR、换做法），或交 Owner 决定。
  每轮都多挑出几个老问题，说明第一轮就该要求按类改全，而不是继续多跑几轮。
- **审查绑定 SHA**：被审 head 之后又有提交，就补一轮增量审查。只有一种例外：无冲突、没带额外改动的 `git merge origin/<目标>`。解过冲突的 merge 要审冲突处。

## 6. 合并纪律

**合并一律通过 PR**（在托管平台上合并），不在本地 merge 后直接推目标分支；要把别的分支带进目标，也开 PR。

三个条件同时满足才合并：

1. **PR checks 全绿。**「存量失败」「本地跑过了」「只改了文档」都不是豁免。
   `no checks reported` 是没跑，不是通过：先查清触发条件和缺失原因，优先让检查在这个 PR 的候选提交上跑起来；不为拿到绿灯改 PR 目标。
2. **源分支包含最新的目标分支**：`git merge-base --is-ancestor origin/<目标> <源>`。落后就在 PR 分支里 `git merge origin/<目标>`（用 merge 不用 rebase，冲突逐段解）。push 前、合并前各重读一次目标分支，不用早先记下的 SHA。
3. **当前 HEAD 审过了**：PR 上有 lead 的审查记录覆盖当前 HEAD（或只差一次无冲突的目标分支 merge），每条 Important 都已修复或写明理由关闭；高档还要 Owner 对这个 HEAD 的同意。
   审查 worker 起不来，不等于审过了：停在 PR，交 Owner。

确实拿不到 CI 时，停在 PR，交给 Owner 决定。Owner 接受这次无 CI 合并时，PR 里写明：候选 HEAD、目标 SHA、为什么没有 CI、
在干净 worktree 实跑的命令与结果、没覆盖到的部分，以及 Owner 针对这个版本的同意。这只批准这一次，不等于 CI 通过；版本变了要重来。
本来就没有 CI 的仓库（如配置、运维类），由 Owner 事先在 README 写定验证门槛和合并授权，lead 照 README 执行，不在每个 PR 重复请示。

不在本地跑完整 CI 当门槛，让 PR 的 CI 去验；本地只跑和改动相关的部分用于定位。
并行的几个 PR 各自绿 ≠ 合起来绿：合并后看目标分支的 CI，并和合并前的失败集合逐条比对。

## 7. 验收判据

- **跨端改动两侧各自绿 ≠ 合起来对**：至少要有契约测试或端到端实跑之一；用可辨认的标记值、抓真实请求、留一个不该变的对照项。
- **验收覆盖 Issue 标题声称的全部范围**；调查发现的缺口要么成为验收项，要么写进「不做什么」。
- **测试绿 ≠ 用户能用**：有界面就做页面验收，有运行环境就看运行态。
- **断言守行为，不守写法**：不写扫源码文本的测试。
- **新加的规则先问是不是既有行为**，新要求找 Owner 确认再写。
- **归因靠对照实验**，不靠同时出现；排查两轮无结论就停止推理，去测量每个环节的实际状态。
- 部署后验实际部署的内容（镜像 tag、版本号），不以「workflow 成功」代替。

## 8. 角色

| 角色 | 负责 | 不负责 |
|---|---|---|
| **Owner**（用户） | 业务决策、范围变更、授权 | — |
| **lead**（主 agent） | 排期、派活、读 checks / 产物验收、核实并处置审查发现、写 PR 审查记录、识别止损 | 写产品代码、替 Owner 答业务问题 |
| **worker** | 一个 Issue 内的实现 | 静默扩大范围、未授权的分支操作（rebase / merge / reset / push） |
| **审查者**（审查 worker） | 按 Issue + `REVIEW.md` 找问题，每条给 `文件:行号` 和失败场景 | 改代码、在 PR 上发言、决定怎么处置 |

需要派 worker 时用 skill `herdr-workers`。汇报时事实标来源，推断标依据和不确定性。

## 9. 防膨胀

- **不为这套流程写工具**：不建 CLI、状态机、本地 backlog、证据库、跨 ws 注册表。Issue 状态从 Issue + PR + checks 现算，不落盘。
- 规则只写一处：通用的在 skill，ws 特有的（参数、目录映射、验收命令、Owner 确认的例外）在它的 README；不复制，只指向。
- 评价一条规则看它有没有改变判断、防住具体错误，不看短期内被加载了几次。
- 一条规则连续几个 Issue 都没帮你做出不同决定，删掉。
- 停掉 herdr / 结束 session / 换 agent CLI，任何一个让交付无法继续，说明耦合过强。

## 10. 新建 ws

1. 问清：ws 名、用途、Issue 开在哪个 repo、涉及哪些 repo 及 remote、能部署到哪。答不上来的写「待定」。
2. 建骨架：

   ```bash
   W=~/workspaces/<名>
   mkdir -p "$W"/{repos,issues,.templates}
   printf 'NOW.md\nrepos/\nissues/\n' > "$W/.gitignore"
   ln -s README.md "$W/CLAUDE.md"
   ```

3. 按第 2 节写 `README.md`（含第 8 项审查），按第 3 节写 `.templates/issue.md`，建空的 `NOW.md`。
   repo 还没有 `REVIEW.md` 的，记成待办：在那个 repo 开 Issue、走 PR 加上，不在建 ws 时顺手提交。
4. clone 只读还是可写的 repo 由用户决定；创建步骤本身不 clone、不拉 worktree、不起 agent。
