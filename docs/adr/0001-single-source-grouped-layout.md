# ADR 0001：单一来源、按场景分组、清单显式、按 SHA 投影

- 状态：已采纳（2026-10-24，Owner 定稿；k8s#75）
- 背景：skill 原来分散在两处——生活助理 18 个在一个私有配置仓库里，经 Open WebUI 原生 skill 和 LiteLLM 公开索引分发；
  开发用 2 个在本仓库手工维护，靠软链装进各 CLI。两份正文会漂移，各消费方也各自接线。

## 决定

1. **本仓库是所有 skill 的唯一来源**，由 agent 直接维护；不从别的仓库生成，也不反向同步
2. 目录 `skills/<组>/<id>/`：一个 skill 只属于一个组，组之间不重叠。初始两组：`assistant`（18 个）、`dev`（2 个）。
   「通用」以后作为 `common` 组单独存在，消费方需要时显式选上；不从正文推断依赖，也不做自动依赖闭包
3. **唯一人工维护的分组清单**是 `.claude-plugin/marketplace.json`（标准 plugin 清单，每组一个 plugin，显式列出 skill，不写 version）；
   不另设 catalog / profiles / VERSION
4. frontmatter `name` = ASCII id = 目录名；中文名放在 `description` 开头
5. 消费方记录 **组列表 + 完整 commit SHA**，用 `scripts/project.py` 把选中的组投影成扁平 view（`<id>/SKILL.md`）：
   部署端是不可变 release + 卷内原子切换 `current`；维护者模式是叶目录软链到本地 checkout（有意动态，不算固定 SHA）
6. 原生 plugin 安装（名字带 `<组>:` 前缀）默认不启用；同一个 CLI 只能在目录方式和 plugin 方式里选一种
7. 公开前门禁：`scripts/preflight.sh`（通用规则 + 仓库外私有词表，扫最终树和每个新提交，fail closed）+ pre-push hook；
   CI 只跑公开规则，并写明私有检查没有运行

## 不做

- 用目录表达生命周期（草稿用 `metadata.status: draft`）
- 仓库里放软链、会删除同名实体目录的安装脚本
- 多套 agent 清单、多处版本号

## 后果

- 新增 skill 必须同时改清单（CI 双向检查）
- npx 不读清单（会发现草稿），只作个人试装入口
- 依赖助理能力的配置（工具名白名单等）留在私有配置仓库，按固定 SHA 检查本仓库内容
