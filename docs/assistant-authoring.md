# assistant 组：写法与约定

（2026-10-24 随 18 个 skill 从 assistant-config `skills/README.md` 迁来；文中 `sync …`、`capabilities/*.yaml`、`prompts/…` 指 assistant-config 仓库，工具名白名单检查仍在那边按固定 SHA 跑）

每个 skill 一个目录，`SKILL.md`（frontmatter `name` / `description` + 正文，附带文件放同目录）是分发给各客户端的全文。
**description 是模型决定加载哪个 skill 的唯一依据**：写清「何时加载」和「依赖谁」。

客户端通常只把所有 skill 的「名称 + 描述」清单给模型，模型需要时再加载全文；各平台的加载方式不同，**skill 正文不依赖其中任何一种**。
本仓库是唯一来源；消费方按组 + commit SHA 用 `scripts/project.py` 安装（见仓库 README）。frontmatter `name` = skill id（目录名，Agent Skills 规范），中文名写在 description 开头（「用户位置：…」），所以 skill 之间仍按中文名互相引用；新增 skill 不需要改模型配置，但要加进 `.claude-plugin/marketplace.json`。删除 skill = 删目录 + 从清单去掉；OWUI 原生 skill 的清理在 assistant-config 用 `python -m sync gc`。

## 写法（v3，Owner 2026-10-20）

- **写方向，不写死**：目标、判断方法、优先顺序、常见坑和怎么识别、停在哪。用「通常 / 优先 / 如果看到 X 说明 Y」的语气，允许 agent 按当前界面变通
- 实测到的界面写成「**参考：2026-10-20 实测时是这样，界面可能改版**」，坐标、控件 id、按钮文字不写成必须
- **硬规则只有 Owner 定的那几条**（下表），其余都是建议
- App skill 建议结构：适用场景 / 入口与定位 / 常见任务怎么做 / 要读出的信息 / 风险与坑 / 停在哪 / 实测记录。没实测过的写「未验证」，不编

## 不限平台、不写死工具名（k8s#75 AC1，Owner 2026-10-23）

- **默认按「服务 + 能力」写，不写工具名**：「手机服务的截图」「坐标转地址服务」「彩云天气服务」「高德地图服务的测距」。
  任何客户端经 LiteLLM 看到的工具名都带它自己加的前缀（OWUI `mcp_phone-…`、Claude Code `mcp__litellm__phone-…`，
  codex / pi 还会把 `-` 改成 `_`：`mcp__litellm__phone_mobile_…`），能力描述在哪都对得上；换 MCP 实现 / 换网关时也不用改 skill
- **例外**：同一个服务里必须分清具体工具时，才写**上游原名**（MCP server 自己的名字），并在第一次出现时给一个叫法，后文用叫法。
  例：手机的「读元素」`mobile_list_elements_on_screen` vs「截图」`mobile_take_screenshot`
- **不写平台专有的东西**：某个客户端的界面（「已分析」、展开 / 折叠、按钮位置）、连接名 / 连接 id、内置工具名（如 `view_skill`）、
  某个 CLI 的命令；写成任何 agent 都适用的说法（「这次调用的结果里」「客户端提供的用户位置」）
- **附带脚本**一律写成 `bash scripts/x.sh` / `python3 scripts/x.py` 调用（分发打包时可执行位会丢）
- `python -m sync validate`：
  - **拦**一切前缀形态：`<连接 id>_<上游名>`、`<server>-<上游名>`、`mcp__<x>__<上游名>`、`<server>_<上游名>`（codex / pi 形态）
  - **拦**工具族（`mobile_…`、`maps_…`）里能力白名单（`capabilities/*.yaml` 的 `litellm.tools`）中不存在的名字
  - **拦**长度：每个工具 `len(serverName) + 1 + len(上游名) ≤ 50`（CLI 的 MCP 配置名固定 `litellm`，加上 `mcp__litellm__` 不超过 64）
  - **警告**：单个文件里上游原名出现超过 8 次（提示改成按能力写）
  - sync 不改写 skill

## 不写个人信息：skill 会公开（k8s#75，Owner 2026-10-23「务必保证 skill 中脱敏」）

本仓库公开，任何人都能读到（含 git 历史）。正文和附带文件里**不写**：手机号、证件号、邮箱、真实地址 /
楼宇 / 小区 / 门牌、用户常在的区或地标、实测时的真实坐标、账号。实测记录里的地点写成泛化说法或公开地标（人民广场，坐标
`31.230, 121.473`）。机器检查：
- push 前 `scripts/preflight.sh`（可信环境）：通用规则 + **仓库外的私有词表**（0600，记录已知的真实地点；不进任何仓库、不打印），
  扫最终的树和每个将要公开的提交；命中、词表不存在或权限过宽都拒绝
- CI：`scripts/validate.py` 的通用规则（`policy/rules.py`），只报文件 / 行 / 类型

## 分层：通用规则只写一处

| 层 | skill | 只写什么 |
|---|---|---|
| 通用 | `location` 用户位置 | **唯一**讲位置的地方：位置来源 / Unknown、坐标转地址服务、坐标系、高德地图服务、App 里「关键词带地标」、**App 里用哪个地址（外卖 / 美食按用户位置，买菜 / 网购问收货地址）**、坐标只发高德 |
| 通用 | `phone-general` 手机通用规则 | 硬规则汇总、什么时候用手机、手机服务 vs 浏览器服务、验证码、怎么看界面（元素读不到就截图）、速度、收尾 |
| 通用 | `shopping` 购物通用 | 比价维度、**到手价识别**（到手价 / 政府补贴 / 会员价 / 支付方式条件价 / 未开始的活动价 / 神券价 / 满减）、规格数量、购物车原有商品、**商品链接（分享 → 复制链接拿直达短链、分享面板禁点清单、拿不到退回搜索链接）**、停在哪 |
| 通用 | `browser-general` 浏览器通用规则 | 一般网页：只在自己开的标签页里操作（pageId）、截图、滚动、等待、扫码登录交接、禁止下单、人机验证停下告诉用户；**淘宝 / 京东不用浏览器** |
| 路由 | `food-delivery` 外卖（手机） | 外卖 / 即时零售选平台、先问什么、收货地址、比价思路、停在哪 |
| App · 网购 | `taobao` 淘宝 | 淘宝 App：搜索 / 商品页多层价格 / 购物车；购物车和分享面板元素读不到（分享面板截图后按坐标点「复制链接」） |
| App · 网购 | `pinduoduo` 拼多多 | 拼多多 App 只读比价：价格口径（单独购买 / 拼单 / 万人团 / 券后 / 百亿补贴）、禁点清单；分享按钮旁的「分享到拼小圈」不点 |
| App · 网购 | `jd` 京东 | 京东 App：回车不提交、卡片「+」是加购、政府补贴 / PLUS / 京东支付价、价保；分享面板（无联系人） |
| App · 二手 | `xianyu` 闲鱼 | 闲鱼 App 只读：搜索输入（剪贴板面板）、结果页 / 详情页读什么、二手靠谱判断（商家 vs 个人、离谱低价、站外交易）；不发消息 / 不出价 / 不点我想要 / 不收藏；分享面板顶上是联系人、复制后弹「去微信粘贴」 |
| App · 本地生活 | `dianping` 大众点评 | 找店、地标搜索、筛选、评价 |
| App · 本地生活 | `meituan` 美团（到店 / 团购） | 美团 App 共性（不用 deep link、冷启动验证、首页结构、热启动回上次页面）+ 到店团购 |
| App · 外卖 | `meituan-waimai` 美团外卖 | 美团 App 里的外卖（独立 App 如有也可）；神券价 |
| App · 即时零售 | `meituan-xiaoxiang` 美团小象超市 | 小象超市入口、商城页、规格单价、生产日期 |
| App · 外卖 | `taobao-flash` 淘宝闪购 | 淘宝首页「闪购」、频道、地址行 |
| App · 外卖 | `jd-miaosong` 京东秒送 | 京东「秒送 / 外卖」、外卖券弹窗 |
| App · 内容 | `weibo` 微博 | 微博 App 只读：热搜（推广位识别）、话题 / 实时、搜索、博主主页、单条和评论；内容判断方向；网页链接 |
| 独立 | `xiaohongshu` 小红书 | 小红书服务、扫码、频率上限 |
| 独立 | `icloud-assistant` iCloud | 日历 / 提醒、什么时候确认、一件事一个日程 + 出发提醒、截图读信息、登录 |

v3 下线：网页 `jd-shopping`、`taobao-shopping`（淘宝 / 京东改走手机 App）；其中的比价、价格陷阱、规格思路迁到 `shopping`，网页专属的选择器 / 价格接口 / 网页滑块 1 次规则随之下线。

## 引用关系（谁依赖谁）

| skill | 依赖 |
|---|---|
| `location` | —（用坐标转地址服务、高德地图服务） |
| `phone-general` | `location`（需要位置时） |
| `shopping` | `location`（收货地址 vs 当前位置）、`phone-general`（停在哪的硬规则） |
| `browser-general` | — |
| `taobao` / `jd` | `phone-general`、`shopping` |
| `xianyu` | `phone-general`、`shopping`（新品价对照用 `taobao` / `jd`） |
| `pinduoduo` | `phone-general`、`shopping` |
| `dianping` | `phone-general`、`location` |
| `meituan` | `phone-general`、`shopping`、`location` |
| `food-delivery` | `phone-general`、`shopping`、`location`；平台 → `meituan-waimai` / `meituan-xiaoxiang` / `taobao-flash` / `jd-miaosong` |
| `meituan-waimai` / `meituan-xiaoxiang` | `phone-general`、`food-delivery`、`shopping`、`meituan`（美团 App 共性） |
| `taobao-flash` | `phone-general`、`food-delivery`、`shopping`、`taobao`（淘宝 App 界面特点） |
| `jd-miaosong` | `phone-general`、`food-delivery`、`shopping` |
| `weibo` | `phone-general` |
| `xiaohongshu` | — |
| `icloud-assistant` | `location`（算路程 / 出发提醒时） |

## Owner 已定的硬规则（出处）

- 位置只用 OWUI 客户端（用户说的地名 / `{{USER_LOCATION}}`），不用手机定位，授权弹窗拒绝，不改系统定位；坐标只发高德；位置细节只写在 `location` —— k8s#17 2026-10-19
- 美团系不用 deep link —— k8s#17 对照实验 2026-10-19
- 验证码 agent 自己完成，同一验证最多 3 次；短信 / 人脸 / 账号风险 / 登录交人 —— k8s#17 PR #22（网页淘宝滑块 1 次的规则随网页 skill 下线，App 内按 3 次）
- 不限制工具调用次数 / 步数；保留的上限只有同一验证 3 次、小红书频率 —— Owner，k8s PR #26
- 绝不提交订单 / 付款；手机任务不用浏览器工具；淘宝 / 京东购物用手机 App；拼多多先不接（2026-10-21 起改为：手机 App 只读比价，网页端仍不用）；skill 不写人名 / 个人连接名 —— Owner 2026-10-19 / 2026-10-20
- 闲鱼只读：不发消息、不出价 / 砍价、不点「我想要」「立即购买」、不下单、不登录、不收藏 —— Owner 2026-10-21（skills v5 追加）
- 外卖等 App 的收货地址要对齐用户位置：可以在**已保存地址里切换**（不新增 / 编辑 / 删除、不设默认）；用户明确指定地址以用户为准；规则只写在 `location` 第 5 节 —— Owner 2026-10-21（v5 追加 2）
- 地址按场景：**外卖、美食 / 到店查询按用户所在位置**；**买菜 / 超市到家、网购问用户送哪个收货地址** —— Owner 2026-10-21（v5 追加 5）
- 拼多多手机 App 只读比价；购物结果给商品链接（不编造 id）—— Owner 2026-10-21（v5 追加 7 / 8）
- 商品链接**默认走 App「分享 → 复制链接」拿直达短链**（淘宝 / 京东 / 拼多多 / 闲鱼，每个列出的商品都给；只贴 URL、不贴口令文案；用完清剪贴板）；分享面板**只点「复制链接」**，联系人 / 渠道 / 海报 / 红包一律不点；拿不到退回搜索链接并注明 —— Owner 2026-10-22（k8s#42，取代 v5 追加 8 的「默认搜索链接、分享复制需 Owner 同意」）
- 微博手机 App 只读（不发不转评赞、不关注私信；用户要求写操作也先不做，交 Owner）；微博不用浏览器 —— Owner 2026-10-21（v5 追加 9）
- **App 类事情一律在手机上做**（淘宝 / 天猫、京东、拼多多、闲鱼、美团、大众点评、淘宝闪购、京东秒送、微博；含给商品链接），浏览器只用于一般网页、不用来打开或核对这些平台的链接；路由写在系统提示 `prompts/assistant-system.md`「Phone or browser」（模型最先读到），skill 只引用、不重复 —— Owner 2026-10-22（k8s#44）
