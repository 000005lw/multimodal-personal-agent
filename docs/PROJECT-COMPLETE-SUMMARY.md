# Whale Personal Agent 完整项目总结

> 项目：`multimodal-personal-agent`
>
> 仓库：https://github.com/000005lw/multimodal-personal-agent
>
> 当前主分支包含可靠性升级和目录整理
>
> 生成日期：2026-11-20

本文是一份面向面试、技术评审和接手开发者的完整项目总结。它不只列出功能，而是说明项目解决什么问题、代码如何分层、一次请求如何运行、状态如何持久化、为什么选择这些工程取舍，以及哪些内容不能夸大为生产成果。

---

## 1. 项目一句话

这是一个接入 QQ 的个人多模态 Agent：它接收 OneBot v11 事件，用 DeepSeek 生成对话，用 SQLite 和 JSON 维护长期状态，用轻量 ActionRuntime 管理模型动作，并通过家庭 Windows worker 处理翻唱、视频抽帧、ASR 等重任务。

它适合一个人加少量朋友的长期陪伴场景，不是通用多 Agent 平台，也不是 QQ 协议实现。

---

## 2. 项目边界与设计约束

### 2.1 实际约束

- 单进程 Python 服务
- 2GB 云服务器
- 主要依赖 `aiohttp`、`numpy`，不引入 Web 框架、ORM、Redis、Celery 或向量数据库
- QQ 协议交给 SnowLuma/NapCat 等 OneBot v11 后端
- 重型媒体任务交给家庭 Windows 电脑
- 运行数据全部保存在本地 `data/`，没有外部状态服务

### 2.2 为什么采用这些约束

项目规模是低并发、单实例、强个人状态场景。引入分布式任务系统或复杂 Agent 框架会增加部署、内存和排障成本，却不能直接改善当前用户价值。因此项目选择：

```text
模型负责理解和生成
确定性代码负责权限、时间、状态、执行和送达
SQLite 负责少量关键状态的持久化
SSH 隧道负责云端和家庭电脑之间的连接
```

---

## 3. 总体架构

```text
手机 QQ
  │
  ▼
OneBot v11 后端（SnowLuma / NapCat）
  │ HTTP 上报
  ▼
whale/bot.py
  ├─ 白名单与身份判断
  ├─ 消息归一化、去重和合并等待
  ├─ 指令、规则、情绪、关系
  ├─ Prompt 拼装
  ├─ DeepSeek / 视觉模型
  ├─ 动作协议与 ActionRuntime
  ├─ 后处理、记忆和发送
  └─ aiohttp HTTP routes
       │
       ├─ 云端 data/
       │   ├─ JSON 状态
       │   ├─ JSONL 归档和 trace
       │   ├─ SQLite 神经元记忆
       │   ├─ SQLite 动作台账
       │   └─ SQLite 家庭任务台账
       │
       └─ GET /jobs/poll + POST /jobs/result
                 │ SSH reverse tunnel
                 ▼
家庭 Windows 电脑
  └─ tools/whale-relay.py
       └─ whale/integrations/home_agent.py
            ├─ B站/抖音读取
            ├─ yt-dlp 下载
            ├─ ffmpeg 抽帧
            ├─ 阿里云 ASR
            └─ 翻唱、人声分离、RVC
```

系统分为三层：

| 层 | 负责内容 |
| --- | --- |
| OneBot 接入层 | 收 QQ 事件、维护登录和协议连接、发送文字/图片/语音 |
| Agent 服务层 | 对话、记忆、关系、规则、权限、动作、任务和持久化 |
| 家庭能力层 | 需要家庭网络或 GPU 的媒体处理 |

---

## 4. 仓库结构

```text
bot.py
  稳定启动包装器

whale/
  bot.py
    aiohttp 服务、事件分发、reply()、Prompt 拼装、后台循环
  config.py
    .env 解析、路径、阈值和功能开关
  agent/
    action_protocol.py
    action_runtime.py
    action_store.py
    job_store.py
    llm.py
  memory/
    memory.py
    neuron.py
  relationship/
    affection.py
    mood.py
    emotion.py
    evolve.py
  behavior/
    rules.py
    remind.py
  media/
    media.py
    imagegen.py
    sing.py
    songlib.py
    stickers.py
    voice.py
  integrations/
    onebot.py
    home_agent.py
    notify.py
    sysinfo.py

scripts/
  selfcheck.py
  build_library.py
  neuron_import.py
  neuron_shadow.py
  neuron_view.py
  neuron_edit.py
  ...

assets/
  personas/
  songs.txt

tools/
  whale-relay.py
  action_replay.py
  reliability_check.py

deploy/
  whale-bot.service
  snowluma.md

tests/
  fixtures/agent_action_scenarios.jsonl

data/
  运行时状态，不进入 Git

docs/
  架构、数据、运维、事故、安全和升级报告

legal/
  MIT.txt
```

---

## 5. 启动与 HTTP 服务

进程入口是根目录 `bot.py`，它只调用 `whale.bot.main()`。这样可以保持 systemd 和旧启动命令兼容。

启动流程：

1. 读取 `.env`。
2. 校验 API key、主人 QQ、端口和人设文件。
3. 创建 `WhaleBot`。
4. 初始化 OneBot、Memory、ActionStore、JobStore 和线程锁。
5. 构建 aiohttp 应用。
6. 注册 startup/cleanup。
7. 启动后台循环。

HTTP 路由：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/`、`/onebot` | OneBot 事件入口 |
| GET | `/health` | 健康、动作和任务状态统计 |
| GET | `/status` | 人类可读状态页 |
| GET | `/jobs/poll` | 家庭电脑领取任务 |
| POST | `/jobs/result` | 家庭电脑提交结果 |
| POST | `/songs/upload` | 上传翻唱片段 |
| GET | `/songs/index` | 查询曲库 |

事件入口收到 JSON 后会立即返回 200，在后台 task 中处理，避免模型或媒体处理导致 OneBot 上报超时。

---

## 6. 一条普通消息的完整链路

```text
handle_event()
  -> 过滤 request/meta/notice
  -> 检查 self_id、user_id、白名单
  -> 去重 message_id
  -> private/group 分流
  -> cid = u<QQ> 或 g<群号>
  -> 提取文本、图片、表情包
  -> 指令处理
  -> 合并等待
  -> reply()
```

进入 `reply()` 后：

1. 每个会话获取独立 `asyncio.Lock`。
2. 更新最后活跃时间和收到消息时间。
3. 处理勿扰、好友管理和管理指令。
4. 检查朋友频率、自动回复和群聊边界。
5. 更新情绪，判断是否生闷气或忽略。
6. 抽取自然语言规则和定时任务。
7. 执行主人专属自查、内部状态查询、点歌和视频许可等短路逻辑。
8. 准备图片、链接和视频上下文。
9. 拼装 system prompt、记忆、关系和事实。
10. 调用 DeepSeek 或视觉模型。
11. 解析动作，必要时执行搜索后的第二次生成。
12. 清洗文本、核实声称、检查话题禁区和朋友隐私。
13. 执行动作与媒体任务。
14. 写入短期记忆、异步学习长期记忆并发送多条气泡。

---

## 7. Prompt 与上下文如何拼装

`build_messages()` 的 system 信息按顺序包括：

1. 主人/朋友保护人设。
2. 关系类别和关系词。
3. 朋友称呼。
4. 语音模式要求。
5. 表情包能力。
6. 长期备注和滚动摘要。
7. 主人性格快照。
8. 好感度、错过提示、吃醋状态。
9. 当前情绪和生闷气说明。
10. 已确认的自然语言规则。
11. 搜索、视频、生图、唱歌和语音能力说明。
12. 真实能力事实、曲库内容、媒体计数和行动台账。
13. 话题间隔提醒。
14. 神经元召回结果。
15. 近期历史。
16. 当前用户文本或图片块。

设计重点：

- 人设只负责语气，不负责权限。
- “我刚唱过”“我发过”等陈述必须能对应真实行动台账。
- 朋友路径不注入主人的梦、性格快照或私密事实。
- 越接近用户消息的系统事实，模型越重视。

---

## 8. 动作协议与 ActionRuntime

### 8.1 解析层

`whale/agent/action_protocol.py` 识别十种动作标记：

```text
search
draw
find_image
song
watch_video
voice
songlist
introspect
sticker
screenshot
```

它只负责解析和清理正文，不执行任何副作用。动作 ID 格式为：

```text
message_id:kind:sequence
```

### 8.2 执行层

`whale/agent/action_runtime.py` 负责：

- 角色授权
- 执行中状态
- 已交付/未确认状态
- 可重试和终止失败
- 重复动作保护
- 超时和未知状态
- 参数脱敏 trace

当前 V2 只注册三类工具：

| 工具 | 允许角色 |
| --- | --- |
| `songlist` | 主人私聊 |
| `introspect` | 主人私聊 |
| `sticker` | 主人私聊、朋友私聊 |

`ACTIONS_V2=false` 仍是默认值。协议支持的动作数量不等于已注册工具数量。

### 8.3 动作台账

`whale/agent/action_store.py` 使用 `data/runtime.db` 保存动作生命周期。服务重启后，上次处于 `in_flight` 的动作会标记为 `unknown`，不会盲目重放可能已经产生外部副作用的操作。

---

## 9. 短期记忆与长期记忆

### 9.1 短期记忆

`memory.py` 维护：

```text
messages   近期逐字对话
summary    滚动摘要
notes      手工长期备注
archive    完整 JSONL 归档
```

超过字符数或消息条数时，先让模型生成摘要，摘要成功后才删除旧窗口；原文归档不会因此丢失。

### 9.2 神经元记忆

`neuron.py` 使用 SQLite 节点和突触：

```text
neuron  节点：事实、偏好、人物、事件、情绪
synapse 突触：共现关系及权重
meta    衰减和迁移元数据
```

检索流程：

1. 对问题生成 embedding。
2. 按 cid 过滤本人的节点。
3. 计算余弦相似度。
4. 用长期强度调制初始激活。
5. 从 top 种子沿突触扩散。
6. 使用相对阈值和绝对阈值过滤。
7. 更新 hit 和遗忘稳定性。

记忆唯一性是 `UNIQUE(cid, text)`，不是全局文本唯一。旧数据库通过 SQLite 表重建迁移，保留节点 ID 和突触引用。

### 9.3 异步 Embedding

真实 Embedding 使用异步 aiohttp 请求，回复热路径通过 `activate_async()` 预先召回。同步接口保留给脚本和兼容路径。网络不可用时仍可使用哈希向量。

---

## 10. 好感度、情绪与规则

### 10.1 好感度

- 范围 0 到 100
- 主人固定 100
- 每日自然增长上限 6
- 长时间不聊会衰减
- 唱歌/看视频门槛 40
- 画画门槛 60
- 低等级会在发送前做确定性降温

### 10.2 情绪

`mood.py` 是关系情绪状态机，控制是否生闷气、忽略消息和急事放行。
`emotion.py` 是表情包选择的 valence/arousal 坐标，不负责关系状态。

明确情绪现在采用确定性分类，避免同一句话被随机映射到相反表情。

### 10.3 规则和定时

`rules.py` 把自然语言要求落成：

- 安静时段
- 不主动
- 最小主动间隔
- 话题禁区
- 禁发表情包

`remind.py` 负责一次性、每天和工作日定时。时间点和“上午/晚上”的歧义由代码校验，不由模型自行计算。

---

## 11. 家庭任务生命周期

媒体任务写入 `data/jobs.db`：

```text
pending
  -> claimed
  -> result_ready
  -> delivering
  -> consumed

pending/claimed
  -> failed
  -> unknown
```

关键机制：

- 任务创建先持久化，再等待内存 Future。
- Worker 领取时使用租约。
- 结果先写入 SQLite，再通知等待者。
- 重复结果不会覆盖已完成结果。
- 过期租约可以在重启或下一次领取时恢复。
- 结果大小有限制，避免 SQLite 无限膨胀。
- 音频结果在没有等待者时可补发。
- ASR token 只在 worker 领取时临时注入，不持久化。

这不是端到端 Exactly Once。外部发送成功但确认丢失时，系统会标记未知，避免自动重复发送。

---

## 12. 多模态能力

| 能力 | 实现 |
| --- | --- |
| 图片理解 | OneBot 图片下载、大小限制、base64 vision block |
| B站/抖音链接 | 专用 API/页面解析，家庭电脑优先 |
| 视频理解 | yt-dlp 下载、ffmpeg 抽帧、可选 ASR |
| 生图 | 通义万相文生图/图生图，支持固定角色参考图 |
| TTS | espeak、阿里云、腾讯云、ZipVoice |
| 唱歌 | B站搜索、下载、demucs、音高判断、RVC、59 秒分段 |
| 搜图 | 必应图片、过滤素材站、可选视觉相关性审核 |
| 表情包 | 按情绪目录选图，结合当前心情和正文审查 |

---

## 13. 数据与持久化

| 数据 | 格式 | 用途 |
| --- | --- | --- |
| `.env` | env | 密钥和部署配置，绝不提交 |
| `data/memory.json` | JSON | 短期窗口和摘要 |
| `data/archive.jsonl` | JSONL | 完整对话归档 |
| `data/neuron.db` | SQLite | 长期记忆 |
| `data/runtime.db` | SQLite | 动作生命周期 |
| `data/jobs.db` | SQLite | 家庭任务 |
| `data/affection.json` | JSON | 好感度 |
| `data/mood.json` | JSON | 关系情绪 |
| `data/emotion.json` | JSON | 表情情绪坐标 |
| `data/rules.json` | JSON | 用户规则 |
| `data/reminders.json` | JSON | 定时 |
| `data/contacts.json` | JSON | 称呼、关系、勿扰 |
| `data/state.json` | JSON | 行动台账、计划和歌曲状态 |
| `data/action_trace.jsonl` | JSONL | 脱敏动作 trace |
| `data/songs/` | JSON + 音频 | 曲库 |

备份时必须整体备份 `data/`，SQLite 最好在停服务后复制。

---

## 14. 安全与隐私

已实现的边界：

- 白名单外用户静默丢弃。
- 私聊和群聊使用不同 cid。
- group 不使用主人私密人设。
- owner-only 能力同时检查私聊和 QQ 身份。
- 朋友输出发送前二次过滤。
- relay token 只接受 `X-Home-Token` 请求头。
- 动作 trace 不记录完整聊天原文或真实 QQ。
- 自由文本动作参数只存哈希和长度。
- 家庭任务结果有大小上限。

仍然存在的风险：

- `data/` 没有静态加密。
- worker 可以调用 ffmpeg、demucs 等外部程序。
- 朋友隐私过滤仍属于黑名单式兜底。
- 单进程 JSON 状态不适合多实例并发。
- 外部平台无法提供真正的端到端发送确认。

---

## 15. 测试与工程验证

```bash
python -W error::SyntaxWarning -m compileall -q bot.py whale scripts tools
python scripts/selfcheck.py
python tools/action_replay.py
python tools/reliability_check.py
```

当前基线：

```text
selfcheck                  159/159
action replay              36/36
reliability checks         15/15
```

可靠性检查覆盖：

- 旧 neuron schema 迁移
- ID 和突触保留
- 跨 cid 同文本隔离
- 失败动作显式重试
- 已交付动作跨重启去重
- 超时进入 unknown
- 自由文本 Trace 脱敏
- 任务领取和结果持久化
- 过期租约恢复
- 重复结果拒绝
- 异步 Embedding 不阻塞其他协程
- URL token 被拒绝

所有检查使用临时数据库和合成数据，不访问真实 QQ、生产库或家庭电脑。

---

## 16. 部署方式

### 云端

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/selfcheck.py
python bot.py
```

生产环境建议 systemd user service，并开启 linger。服务模板在 `deploy/whale-bot.service`。

### QQ 桥接

推荐 SnowLuma Docker 容器，HTTP API 绑定 `127.0.0.1:3001`，事件上报到 bot 的 `8090`。首次登录需要扫码。

### 家庭电脑

需要 SSH 免密、Python、yt-dlp、ffmpeg；唱歌还需要 demucs/RVC 相关依赖。启动 `python tools/whale-relay.py` 后，它会维持反向隧道并轮询任务。

---

## 17. 本次二次开发的主要贡献

### 已有基础

原项目已经实现 OneBot 接入、人格、记忆、好感度、规则、多模态和家庭中继。

### 本次新增或强化

1. 修复跨用户记忆写入隔离并实现旧库迁移。
2. 重构 ActionRuntime 状态和交付语义。
3. 增加 SQLite 动作台账和跨重启防重放。
4. 增加 SQLite 家庭任务台账、租约和恢复。
5. 将 Embedding 热路径改为异步。
6. 移除 URL query token 认证。
7. 加强 Trace 参数脱敏。
8. 修复情绪测试的随机失败。
9. 重组 Python 包、脚本、资源、文档和许可证目录。
10. 重写 README 和交接/升级文档。
11. 新增可靠性检查并接入 GitHub Actions。

---

## 18. 可以写进简历的保守表述

可以在核对后使用：

> 参与开源多模态 QQ 个人 Agent 的二次开发与可靠性优化，负责动作协议、工具运行时、SQLite 状态台账、任务恢复和工程测试；相关改动已通过 PR/主分支和 GitHub Actions 验证。

也可以拆成三个方向：

- **Tool Calling**：解析模型动作，做权限、状态、重试、交付和 Trace。
- **异步任务编排**：SQLite 任务台账、worker 租约、结果持久化和重启恢复。
- **数据与测试**：跨用户记忆迁移、异步 Embedding、离线回放和故障注入。

不要写：

- 从零独立开发整个项目。
- 生产环境实现 Exactly Once。
- 所有工具真实调用成功率 100%。
- 已完成商业级分布式任务平台。

---

## 19. 面试时最值得讲清楚的问题

1. 为什么要区分执行成功和消息交付成功？
2. 发送成功但确认丢失时为什么不能自动重试？
3. 为什么选用 SQLite，而不是 Redis 或 Celery？
4. 为什么动作 ID 去重不能简单放在内存？
5. 为什么记忆唯一约束必须包含 cid？
6. 为什么旧 SQLite schema 要重建表而不是直接改约束？
7. 家庭任务租约如何防止两个 worker 重复领取？
8. 为什么同步 Embedding 会阻塞单进程 aiohttp？
9. 为什么保留旧动作业务路径，不一次性迁移全部工具？
10. 如何证明改动没有破坏原有功能？

---

## 20. 当前限制

- `whale/bot.py` 仍然很大，后续应做局部提取而不是一次性重写。
- 动作台账和任务台账是两个 SQLite，没有统一跨表事务。
- worker 没有 heartbeat，依赖较长租约。
- 非音频任务在等待者消失后不会自动重建原业务回调。
- 部分状态仍是 JSON 全量覆盖写。
- 没有生产环境 P95/RSS 数据，不能虚构性能提升。

---

## 21. 推荐阅读顺序

1. 本总结
2. `README.md`
3. `whale/bot.py` 的 `handle_event()`、`reply()`、`build_messages()`
4. `whale/agent/action_protocol.py`
5. `whale/agent/action_runtime.py`
6. `whale/agent/action_store.py`
7. `whale/agent/job_store.py`
8. `whale/memory/memory.py`
9. `whale/memory/neuron.py`
10. `whale/behavior/rules.py` 和 `remind.py`
11. `whale/integrations/home_agent.py` 和 `tools/whale-relay.py`
12. `scripts/selfcheck.py` 和 `tools/reliability_check.py`
