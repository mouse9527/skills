# skills

mouse9527 的 agent skill，**所有 skill 的唯一来源**（Claude Code / Codex / Pi，以及 Open WebUI 的 Open Terminal）。
由 agent 直接在这里维护；规则见 [AGENTS.md](AGENTS.md)，结构决策见 [docs/adr/](docs/adr/)。

## 结构

- `skills/<组>/<id>/SKILL.md`：一个 skill 只属于一个组；`name` = `<id>`（ASCII），中文名写在 `description` 开头
- 组 = [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json) 里的一个 plugin，**显式**列出组内 skill；组之间不重叠
- `scripts/project.py`：按「组列表 + 完整 commit SHA」把 skill 投影成扁平目录 `<id>/SKILL.md`（供各客户端发现）

## 安装

消费方记录 **组列表 + 完整 commit SHA**，用 `scripts/project.py` 安装，不直接把客户端指向本仓库根目录
（skill 在 `skills/<组>/` 下，客户端只扫描一层）：

```bash
# 部署（不可变 release，按 SHA；失败保留上一份；可回滚）
python3 scripts/project.py release --github mouse9527/skills --ref <sha|main> --groups assistant --root /opt/llm-skills
#   → /opt/llm-skills/current/view/<id>/ ；客户端的 skill 目录链到 current/view
python3 scripts/project.py rollback --root /opt/llm-skills
# CLI（Claude Code 读 ~/.claude/skills；Codex / Pi 读 ~/.agents/skills）：release 后把叶目录链到 current，循环里保持同步
python3 scripts/project.py release --github mouse9527/skills --ref main --groups dev,assistant \
  --root ~/.local/share/agent-skills --link ~/.agents/skills --link ~/.claude/skills --loop 300

# 维护者模式（本地 checkout，改动立即可见，不固定 SHA）：每个 skill 一条叶目录软链
python3 scripts/project.py link --repo ~/workspaces/skills --groups dev --dest ~/.claude/skills
```

`link` 只管理自己建过的链接（记在 `<dest>/.skills-project.json`），遇到同名的其他文件 / 链接直接失败、不覆盖。

个人试装也可以用 `npx skills add mouse9527/skills/skills/<组>`。注意 npx 不读 marketplace 清单，会把草稿也装上。
原生 plugin（`claude plugin install <组>@mouse9527-skills`，skill 名带 `<组>:` 前缀）目前**不启用**；
同一个 CLI 只能二选一，避免重复加载。

## 校验与脱敏

本仓库公开。push 前必须在可信环境跑 `scripts/preflight.sh`（通用规则 + 仓库外的私有词表，缺词表即拒绝）；
CI 只能跑公开规则：`agentskills validate`、`scripts/validate.py`、`tests/`。**CI 绿不代表私有检查通过。**

## 索引

<!-- skills-index:start -->

| group | skill | |
|---|---|---|
| assistant | [browser-general](skills/assistant/browser-general/SKILL.md) | 浏览器通用规则 |
| assistant | [dianping](skills/assistant/dianping/SKILL.md) | 大众点评（手机） |
| assistant | [food-delivery](skills/assistant/food-delivery/SKILL.md) | 外卖（手机） |
| assistant | [icloud-assistant](skills/assistant/icloud-assistant/SKILL.md) | iCloud 日程与提醒 |
| assistant | [jd](skills/assistant/jd/SKILL.md) | 京东（手机 App） |
| assistant | [jd-miaosong](skills/assistant/jd-miaosong/SKILL.md) | 京东秒送（手机） |
| assistant | [location](skills/assistant/location/SKILL.md) | 用户位置 |
| assistant | [meituan](skills/assistant/meituan/SKILL.md) | 美团（手机 |
| assistant | [meituan-waimai](skills/assistant/meituan-waimai/SKILL.md) | 美团外卖（手机） |
| assistant | [meituan-xiaoxiang](skills/assistant/meituan-xiaoxiang/SKILL.md) | 美团小象超市（手机） |
| assistant | [phone-general](skills/assistant/phone-general/SKILL.md) | 手机通用规则 |
| assistant | [pinduoduo](skills/assistant/pinduoduo/SKILL.md) | 拼多多（手机 App） |
| assistant | [shopping](skills/assistant/shopping/SKILL.md) | 购物通用 |
| assistant | [taobao](skills/assistant/taobao/SKILL.md) | 淘宝（手机 App） |
| assistant | [taobao-flash](skills/assistant/taobao-flash/SKILL.md) | 淘宝闪购（手机） |
| assistant | [weibo](skills/assistant/weibo/SKILL.md) | 微博（手机 App） |
| assistant | [xianyu](skills/assistant/xianyu/SKILL.md) | 闲鱼（手机 App） |
| assistant | [xiaohongshu](skills/assistant/xiaohongshu/SKILL.md) | 小红书 |
| dev | [herdr-workers](skills/dev/herdr-workers/SKILL.md) | 用 herdr 把任务交给另一个 agent（worker）时的布局、启动、派发… |
| dev | [workspace](skills/dev/workspace/SKILL.md) | 工作区（ws）的骨架、创建、日常使用与收尾规则。所有 ws 放在 ~/works… |

<!-- skills-index:end -->
