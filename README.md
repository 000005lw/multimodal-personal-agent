# Whale Personal Agent

一个面向 QQ 私聊和少量好友场景的个人多模态 Agent。项目基于 **Python 3.10+、aiohttp、OneBot v11、DeepSeek API 和 SQLite**，重点处理长期记忆、人格状态、工具调用权限、动作生命周期以及云端与家庭电脑之间的异步任务协作。

项目不是通用多智能体框架，也不实现 QQ 协议本身。它刻意保持单进程、低依赖和可人工排障的形态，适合一个人加少量朋友的长期陪伴场景。

## 核心能力

- **对话与人格**：DeepSeek 对话、人设文件、关系状态、好感度、情绪和主动消息。
- **长期记忆**：近期对话窗口、滚动摘要、SQLite 神经元记忆、embedding 检索和遗忘曲线。
- **Agent 工具运行时**：动作协议、身份权限、动作状态、交付确认、失败重试和脱敏 trace。
- **多模态**：图片理解、文字转语音、翻唱、视频抽帧、语音识别、搜索和生图。
- **家庭电脑协作**：云端通过 HTTP 任务队列和 SSH 隧道把重任务交给家庭 Windows worker。
- **可靠性**：SQLite 动作台账、家庭任务租约、重启恢复、重复结果去重和离线故障注入测试。

## 系统结构

```text
QQ
  -> OneBot v11 后端（SnowLuma / NapCat）
  -> aiohttp 事件入口
  -> 身份、规则、情绪和权限检查
  -> DeepSeek / 多模态模型
  -> 动作协议和 ActionRuntime
  -> OneBot 发送 / 媒体任务

云端服务器
  ├─ 对话、记忆、规则、动作和任务调度
  ├─ data/ JSON、JSONL、SQLite 和媒体文件
  └─ HTTP task ledger
           ↕ SSH tunnel
家庭 Windows 电脑
  └─ B站/抖音读取、视频抽帧、ASR、唱歌和 RVC
```

## 仓库目录

```text
bot.py                        稳定启动入口，兼容 systemd 和旧启动命令
whale/
  bot.py                      aiohttp 服务、事件分发和回复主流程
  config.py                   配置加载与路径定义
  agent/                      动作协议、ActionRuntime、动作/任务台账、LLM
  memory/                     短期对话记忆和神经元长期记忆
  relationship/               好感度、情绪、表情包情绪和性格快照
  behavior/                   自然语言规则和定时提醒
  media/                      语音、唱歌、图片和媒体处理
  integrations/               OneBot、家庭 worker、邮件和系统自检
scripts/                      selfcheck、曲库、记忆工具和运维脚本
assets/
  personas/                   主人/朋友人设
  songs.txt                   批量曲库清单
tools/                        家庭中继和离线回放工具
deploy/                       systemd 与 OneBot 部署材料
docs/                         架构、数据模型、运维、安全和升级报告
legal/                        许可证正文
tests/                        合成动作场景
data/                         运行时状态，默认不进入 Git
```

## 快速开始

```bash
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

至少填写：

```env
OWNER_QQ=你的 QQ 号
DEEPSEEK_API_KEY=你的 DeepSeek key
ONEBOT_API_BASE=http://127.0.0.1:3001
ONEBOT_ACCESS_TOKEN=与 OneBot 后端一致
```

启动 OneBot 后端和机器人：

```bash
docker run -d --name snowluma --restart unless-stopped \
  -p 127.0.0.1:3001:3001 \
  -e ONEBOT_ACCESS_TOKEN=与 .env 一致 \
  motricseven7/snowluma:latest

python scripts/selfcheck.py
python bot.py
```

Windows 也可以双击 `scripts/run-bot.bat`。

## 配置

完整配置见 [.env.example](.env.example)。常用配置分为：

- 白名单：`OWNER_QQ`、`FRIEND_QQ`、`REPLY_TO_GROUPS`
- OneBot：`ONEBOT_API_BASE`、`ONEBOT_ACCESS_TOKEN`、`BOT_HOST`、`BOT_PORT`
- 对话：`DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`、视觉模型
- 记忆：`HISTORY_TURNS`、`MAX_HISTORY_CHARS`、`RUNTIME_DB_FILE`
- 家庭中继：`HOME_TOKEN`、`HOME_RELAY_URL`、`HOME_MIN_VERSION`
- 动作运行时：`ACTIONS_V2`、`ACTION_TRACE_ENABLED`
- 任务台账：`JOB_DB_FILE`、`JOB_LEASE_SECONDS`、`JOB_MAX_ATTEMPTS`

`ACTIONS_V2` 默认关闭。未注册动作继续走原有业务路径，不能在文档或简历中把协议支持的动作数量等同于已接入运行时工具数量。

## Agent 工具运行时

工具动作先由 `whale/agent/action_protocol.py` 转换成结构化动作，再由 `whale/agent/action_runtime.py` 做：

1. 角色授权：主人私聊、朋友私聊、群聊、主动生成。
2. 动作状态：执行中、已交付、未确认、可重试失败、未知、拒绝。
3. 交付确认：执行成功和外部消息实际送达分开记录。
4. 单进程/跨重启动作台账：`data/runtime.db`。
5. 参数脱敏：自由文本参数只保留哈希和长度。

当前 V2 注册工具只有：

- `songlist`：仅主人私聊
- `introspect`：仅主人私聊
- `sticker`：主人私聊、朋友私聊

Agent 动作生命周期状态保存到 SQLite 后，服务重启时中断动作会标记为 `unknown`，不会盲目重放可能已经产生副作用的操作。

## 家庭任务

长时间媒体任务写入 `data/jobs.db`，状态包括：

```text
pending -> claimed -> result_ready -> delivering -> consumed
                    \-> failed / unknown
```

家庭 worker 通过 `GET /jobs/poll` 原子领取任务，领取时写入租约。结果通过 `POST /jobs/result` 持久化，重复结果不会覆盖已完成任务。服务重启后可以恢复过期租约；音频结果在原等待者消失时可以补发。

这不是端到端 Exactly Once。发送成功但确认丢失时，系统会保留 `unknown`，避免自动重复发送。

## 开发与测试

```bash
python -W error::SyntaxWarning -m compileall -q bot.py whale scripts tools
python scripts/selfcheck.py
python tools/action_replay.py
python tools/reliability_check.py
```

当前基线：

```text
selfcheck                 159/159
action replay             36/36
reliability checks        15/15
```

测试全部使用临时数据库和合成数据，不连接真实 QQ、DeepSeek、家庭电脑或生产数据库。

## 数据与隐私

绝对不要提交：

- `.env`、API key、OneBot token、邮箱密码
- `data/`、真实聊天、QQ 号、联系人、音频和日志
- `data/neuron.db`、`data/runtime.db`、`data/jobs.db` 等运行台账

至少 `data/` 需要定期加密备份。当前台账仍是敏感数据，和聊天数据同等保护。

## 部署

Linux 推荐使用 systemd user service：

```bash
cp deploy/whale-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now whale-bot
loginctl enable-linger "$USER"
```

家庭中继：

```powershell
python tools\whale-relay.py
```

详细步骤见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

## 文档

- [交接总览](docs/HANDOVER.md)
- [完整项目总结](docs/PROJECT-COMPLETE-SUMMARY.md)
- [系统架构](docs/ARCHITECTURE.md)
- [数据模型](docs/DATA-MODEL.md)
- [运维手册](docs/OPERATIONS.md)
- [事故记录](docs/LESSONS.md)
- [本轮升级报告](docs/UPGRADE-REPORT-2026-11-18.md)
- [可靠性交接](docs/RELIABILITY-HANDOVER.md)
- [开发指南](docs/DEVELOPMENT.md)
- [安全模型](docs/SECURITY-MODEL.md)

## 许可证

本项目使用 MIT License，许可证正文见 [legal/MIT.txt](legal/MIT.txt)。原项目版权声明保留。

## 当前限制

- `whale/bot.py` 仍然较大，尚未完成全量模块化。
- 当前是单进程架构，不支持同一数据目录上运行多个 bot 实例。
- 部分 JSON 状态仍采用整体覆盖写，适合小规模个人部署。
- 动作台账和家庭任务台账是两个 SQLite 文件，不是统一事务。
- 家庭 worker 目前只有租约，没有独立 heartbeat。
- 不宣称生产环境端到端 Exactly Once。
