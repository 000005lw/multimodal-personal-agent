# multimodal-personal-agent

把大模型接到 QQ 上的**私人陪伴型 agent**。单进程 Python，一个人 + 几个朋友的私聊场景。

- 有人格（`persona.md`），会撒娇、会闹别扭、会吃醋
- 有长期记忆：神经元网络 + 遗忘曲线（会被想起，也会慢慢淡）
- 有边界：好感度门禁、作息规则、话题禁区、隐私隔离
- 有多模态：看图、唱歌（换声）、看 B 站视频、发表情包、语音
- 有主动性：会在合适的时候自己开口，也会在对方 5 天没回时安静下来

## 快速开始

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # 填 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / ONEBOT_* / OWNER_QQ
# 使用下面的 SnowLuma 容器时，设置 ONEBOT_API_BASE=http://127.0.0.1:3001
docker run -d --name snowluma -p 127.0.0.1:3001:3001 \
  -e ONEBOT_ACCESS_TOKEN=<同 .env> motricseven7/snowluma:latest
python selfcheck.py         # 159 项，离线，不花 token
python tools/action_replay.py  # 36 条合成 Agent 动作场景
python tools/reliability_check.py  # 记忆隔离、动作失败、任务恢复
python bot.py
```

## 文档

| 文档 | 看它做什么 |
| --- | --- |
| [docs/HANDOVER.md](docs/HANDOVER.md) | **从这里开始**：全貌、模块地图、六条不变量、优化路线 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 进程内结构、两条消息路径、家里中继协议、时序图 |
| [docs/DATA-MODEL.md](docs/DATA-MODEL.md) | 所有数据结构的字段与算法（记忆/好感/情绪/规则/定时/曲库） |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 部署、systemd、docker、排障手册、备份 |
| [docs/LESSONS.md](docs/LESSONS.md) | 13 个真实事故与教训（最值钱的部分） |
| [docs/RELIABILITY-HANDOVER.md](docs/RELIABILITY-HANDOVER.md) | 本轮可靠性升级的交接说明 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 改代码的流程与提交规范 |
| [SECURITY.md](SECURITY.md) | 密钥与隐私边界，绝不能提交的东西 |

## 结构

```
bot.py            主干：事件 → 分发 → 生成 → 后处理 → 发送
config.py         所有配置与阈值（全部走 .env）
persona*.md       人设（改完重启即生效）
action_protocol.py 旧式动作标记的纯解析层（无副作用）
action_runtime.py 三工具权限策略、动作生命周期与脱敏 trace
action_store.py  SQLite 动作台账：失败、未知和已交付状态
job_store.py      SQLite 家庭任务台账：租约、结果和恢复
neuron.py         记忆：节点/突触/扩散激活/遗忘曲线
memory.py         短期窗口 + 摘要压缩
affection.py      好感度：分档、衰减、门禁、行为后处理
mood.py           瞬时情绪与生闷气
rules.py          自然语言 → 结构化规则（作息/话题/定时）
remind.py         定时队列
sing.py           唱歌流水线（在家里电脑执行）
songlib.py        曲库：已做好的切片缓存
home_agent.py     家里电脑的 worker（领活/交活）
selfcheck.py      离线自测（159 项）
```

## Agent 工具运行时

模型动作会先由 `action_protocol.py` 转成结构化请求，再由
`action_runtime.py` 做身份权限校验、执行分发、生命周期管理和脱敏 trace。
动作状态写入 `data/runtime.db`，服务重启后中断动作会标记为 `unknown`，
不会自动重放可能已经产生外部副作用的操作。

为保证向后兼容，新运行时默认关闭：

```env
ACTIONS_V2=false
```

当前接入的三个工具：

- `songlist`：仅主人私聊
- `introspect`：仅主人私聊
- `sticker`：主人私聊、朋友私聊

测试和 canary 时可设置为 `ACTIONS_V2=true`。未注册动作仍走原业务路径，
不会绕过已有的好感度、禁发表情包和隐私检查。

## 家庭任务可靠性

云端与家庭电脑之间的长耗时任务使用 `data/jobs.db` 保存：

- `pending`
- `claimed`
- `result_ready`
- `delivering`
- `consumed`
- `failed`
- `unknown`

家庭电脑领取任务时会获得租约。服务重启后，过期租约可以被恢复；
重复结果不会覆盖已完成任务；结果未知时不会盲目重新执行外部副作用。

## 依赖

核心必需依赖为 `aiohttp` 和 `numpy`，由 `requirements.txt` 安装。

可选依赖：

- `httpx`：百炼真实 embedding；缺失时退化为哈希向量
- `librosa` + `torch` + `demucs`：仅家里电脑唱歌流水线

## 许可

MIT，见 [LICENSE](LICENSE)。
