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
docker run -d --name snowluma -p 127.0.0.1:3001:3001 \
  -e ONEBOT_ACCESS_TOKEN=<同 .env> motricseven7/snowluma:latest
python selfcheck.py         # 123 项，离线，不花 token
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
| [CONTRIBUTING.md](CONTRIBUTING.md) | 改代码的流程与提交规范 |
| [SECURITY.md](SECURITY.md) | 密钥与隐私边界，绝不能提交的东西 |

## 结构

```
bot.py            主干：事件 → 分发 → 生成 → 后处理 → 发送
config.py         所有配置与阈值（全部走 .env）
persona*.md       人设（改完重启即生效）
neuron.py         记忆：节点/突触/扩散激活/遗忘曲线
memory.py         短期窗口 + 摘要压缩
affection.py      好感度：分档、衰减、门禁、行为后处理
mood.py           瞬时情绪与生闷气
rules.py          自然语言 → 结构化规则（作息/话题/定时）
remind.py         定时队列
sing.py           唱歌流水线（在家里电脑执行）
songlib.py        曲库：已做好的切片缓存
home_agent.py     家里电脑的 worker（领活/交活）
selfcheck.py      离线自测（123 项）
````

## 依赖

必需只有 `aiohttp`。可选：`httpx`（embedding）、`numpy`（向量）、
`librosa`+`torch`+`demucs`（仅家里电脑唱歌用）。

## 许可

MIT，见 [LICENSE](LICENSE)。
