# 维护规则（agent 与人都适用）

本仓库是 mouse9527 **所有 skill 的唯一来源**，由 agent 直接维护。**它是公开仓库**：提交的每一行（包括已删除内容的历史）任何人都能读到。

## 不变量（`scripts/validate.py` 在 CI 检查）

1. 一个 skill = `skills/<组>/<id>/SKILL.md`（附带文件放同一目录）；一个 skill 只属于一个组；`<id>` 全局唯一
2. frontmatter 只用 Agent Skills 规范的字段；`name` = `<id>`（小写 ASCII + `-`）；中文名写在 `description` 开头（「用户位置：…」）
3. 组 = `.claude-plugin/marketplace.json` 里的一个 plugin：`source: "./"`、`strict: false`、**不写 `version`**，`skills` 显式列出组内每个 skill
   - 新增 skill 要加进清单；没列入的必须标 `metadata.status: draft`（草稿也是公开的，同样要脱敏）
   - 不建空组；组之间不重叠
4. **全仓库禁止软链**（软链只由安装器生成）；不放二进制文件
5. `assistant` 组的 skill 每个客户端都会加载：不写客户端 / 网关的名字，也不写带客户端前缀的工具名（只写上游工具名）
6. README 的索引由脚本生成：改了 skill 跑 `python3 scripts/validate.py --write-readme`
7. 外部导入的 skill 记 `metadata.upstream`（来源和版本）

## 公开前（每次 push 之前，必须）

```bash
git config core.hooksPath scripts/hooks     # 一次性：pre-push 只放行 preflight 通过的那个提交
scripts/preflight.sh                        # 工作区干净 → validate → tests → PII 扫描（树 + 每个新提交）→ 记录通过的 SHA
git push
```

- 私有词表（真实地址 / 楼名 / 区 / 地标等）在**仓库外**：`$SKILLS_PII_TERMS`，默认 `~/.config/assistant-config/pii-terms.txt`，0600。
  **不进任何仓库、不打印、不贴进 issue / PR / 提交信息**；缺失、权限过宽、为空都算失败
- 发现个人信息：不要 push。改写后用新提交覆盖并**不能**解决已经 push 的问题（历史仍公开），所以必须在 push 前拦住
- 提交身份只用 GitHub noreply（`policy/rules.py` 的 `COMMIT_IDENTITIES`）；本机默认身份会带出主机名
- CI 拿不到私有词表：**CI 绿不代表私有检查通过**

## 写 skill

- `assistant` 组的写法、分层和依赖见 [docs/assistant-authoring.md](docs/assistant-authoring.md)
- 正文按「何时加载 / 怎么做 / 停在哪」写；没实测过的写「未验证」
- 消费方按完整 commit SHA 安装（见 README）；行为变化写在 PR 说明里，不设版本号文件

## 决策记录

`docs/adr/`。改结构（分组、清单、安装方式、校验）先写 ADR。
