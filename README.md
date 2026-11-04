# 多模态个人智能体（Multimodal Personal Agent）

> 一个长期在线运行的个人智能体：**神经元式长期记忆 + 多模态生成与理解 + 工具调用编排 + 单进程部署**。
> 不是 demo——每天真实在跑，有自测、有监控、有容灾。

## 架构

```
即时通讯接入层（OneBot v11 / HTTP）
        │
        ▼
   单进程 asyncio 事件循环（bot.py）
        │
        ├── 记忆层   neuron.py      记忆节点 + 突触、扩散激活召回、FSRS 遗忘曲线
        ├── 生成层   llm.py         提示词分层（人设 / 事实 / 记忆注入）、工具路由
        ├── 多模态   voice.py / imagegen.py / media.py
        ├── 状态层   affection.py / emotion.py / mood.py / evolve.py
        └── 运维层   notify.py / sysinfo.py / home_agent.py（反向轮询中继）
```

## 核心模块

| 模块 | 职责 |
| --- | --- |
| `neuron.py` | 神经元式记忆库：节点 / 突触建模，扩散激活召回，赫布式共现连边，FSRS 遗忘曲线调度复习 |
| `neuron_view.py` | 记忆网络可视化（导出 3D 力导向图，单文件 HTML） |
| `neuron_shadow.py` | 影子评测：用真实的下一句回复做盲测，对比"激活召回"与"随机抽样"的语义相关度 |
| `voice.py` | 零样本语音克隆（ZipVoice ONNX int8，按标点切段 + 静音拼接） |
| `imagegen.py` | 文生图 / 参考图编辑（锁角色一致性） |
| `media.py` | 视频理解：下载 → 抽帧 → ASR 转写 |
| `home_agent.py` | 反向轮询中继：云服务器排队任务，家里电脑取走执行（绕开机房 IP 限流） |
| `selfcheck.py` | 离线自测套件（不连真实账号、不消耗 token） |

## 关键设计

1. **记忆不是关键字检索**：一条记忆被想起，会顺着突触把"经常一起出现"的关联记忆一起带出来；召回按"相对阈值 + 绝对阈值"双门槛截断，宁可返回 0 条也不塞噪音。
2. **遗忘曲线用 FSRS 而不是指数衰减**：每条记忆维护稳定性 S，成功回忆会拉长 S（间隔效应），想不起来则衰减，每日定时巩固与复习。
3. **单进程、单依赖**：整个机器人只用 `aiohttp`，不做插件化——单人使用场景下框架是过度设计。
4. **可观测**：看门狗巡检 + 邮件告警 + 自动恢复；中继带代码版本握手，避免"改了代码忘了重启"。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env      # 填入你自己的 API Key、OneBot 地址等
python selfcheck.py       # 先跑离线自测
python bot.py
```

`.env` 里需要配置：LLM API（地址 / Key / 模型）、OneBot 接入地址与 token、可选的 TTS / 图像 / 中继配置。

## 数据与隐私

本仓库**只包含代码**：

- 不含任何聊天记录、记忆库、联系人、日志（`data/` 已在 `.gitignore` 中排除）
- 不含真实 API Key、服务器地址、账号信息（配置全部走环境变量）
- 人设文件 `persona.md` 是提示词设计示例，可替换为你自己的

## 技术栈

Python 3.10 · asyncio · aiohttp · SQLite · OneBot v11 · ONNX Runtime · ZipVoice / 通义万相 · FFmpeg · Docker / systemd
