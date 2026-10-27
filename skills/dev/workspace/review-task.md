# 审查任务书模板

lead 复制到 `issues/<issue-id>/review/TASK-R<轮>-<审查者>.md`，填完尖括号再派。流程和处置见 SKILL.md 第 5 节。

---

# 审查 <owner>/<repo>#<PR>（Issue <owner>/<repo>#<issue-id>，第 <轮> 轮，<档位>档）

STATUS 写在交付文件首行，随进度更新。

## 审什么

- Issue：`gh issue view <issue-id> -R <owner>/<repo>`，读目标、验收标准、不做什么
- 被审范围：`base <完整 SHA>` .. `head <完整 SHA>`
  （第 2 轮起：只审 `<上轮 head>..<新 head>`，并核对上轮报告里标「修」的 Important 是否真修了；已关闭的条目见 PR 审查记录，不再提）
- 审查规则：`<repo 路径>/REVIEW.md`（没有就写「无」，只按 Issue 和下面的标准）；repo 的 `AGENTS.md` / `CLAUDE.md`

## 怎么看代码

- 只读。用 `git -C <repos/<repo> 绝对路径> diff <base> <head>`、`git show <head>:<路径>`、`git grep <模式> <head>` 看固定版本，不依赖任何工作区当前内容。
- 需要跑测试确认时，只在自己的分离 worktree 里跑：
  `git -C <repos/<repo>> worktree add --detach <ws>/issues/<issue-id>/review/<repo>-<head 前 8 位> <head>`，用完 `worktree remove`。
- **先独立形成结论**，写完报告后才可以读实现者的报告或 PR 描述里的自述，再补一节「与实现者说法的出入」。

## 找什么

按 REVIEW.md 的优先级；它没写到的，按这个顺序：

1. 改动是否真的达成 Issue 的目标和每条验收标准；bug 修复是否修了根因
2. 会弄坏什么：调用方、存量数据、并发、错误处理、跨端契约
3. 测试是否守住了行为（而不是守写法），缺了哪条能区分对错的测试
4. 文档、注释和代码是否互相矛盾

不报：纯风格、格式化、lint 能抓的、「可以考虑」类的建议。Nit 最多 5 条。

## 交付物

`<ws>/issues/<issue-id>/review/R<轮>-<审查者>.md`，格式：

```
STATUS: in-progress | done | blocked（原因）
head <完整 SHA>：<N> Important / <M> Nit / <K> Pre-existing   （或：head <SHA>：没有需要修复的问题）

## Important
### I1 <一句话结论>
- 位置：`<文件>:<行号>`
- 失败场景：<具体输入 / 状态> → <错误结果>
- 证据：<读到的代码、跑的命令与输出>
- 类别：<问题类别>；同类实例：`<文件:行号>`, …（没有其他实例就写「仅此一处」）

## Nit
## Pre-existing（不是本 PR 引入；能定位就写出引入它的提交）
## 与实现者说法的出入
## 没覆盖到的
```

每条 Important 都必须有 `文件:行号` 和失败场景；写不出就不是 Important。

## 边界

- 能写：只有上面的交付文件和自己的分离 worktree
- 禁止：改被审 worktree 或 `repos/` 本体、commit、push、在 GitHub 上评论或审批、部署、写远端
- 自己做，不要起子 agent / fork；小步落盘，<N> 分钟内先写出结论，拿不准的标「待核」
- 需要 Owner 决定的问题写进交付文件，不自己定
