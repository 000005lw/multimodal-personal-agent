# 交接文档（HANDOVER）

> 版本：2026-11-09 · 对应仓库 `multimodal-personal-agent` main 分支
> 读者：接手本项目的开发者（你）以及任何新开的一次对话
> **先说结论**：这是一个**把大模型接到 QQ 上的私人陪伴型 agent**，单进程 Python，
> 跑在一台 2G 的云服务器上；重活（唱歌换声、抓 B 站视频）由一台家用电脑通过 SSH 隧道代跑。
> 代码 6200 行的 `bot.py` 是主干，其余 30 多个模块是它的器官。

---

## 0. 三分钟速览

| 问题 | 答案 |
| --- | --- |
| 它是什么 | 一个叫「小鲸鱼 / 米米」的 QQ 机器人：私聊、有人格、有长期记忆、会唱歌、会看视频、会主动找人说话 |
| 谁能用它 | 主人（唯一）+ 白名单里的朋友；**其他人静默丢弃**，连「我不认识你」都不回 |
| 技术栈 | Python 3.10 + aiohttp（唯一必需依赖）+ sqlite3 + json 文件；没有 Web 框架、没有数据库服务、没有 ORM |
| 大模型 | DeepSeek `deepseek-chat`（对话）、百炼 `text-embedding-v3`（记忆向量）、百炼图像/语音（可选） |
| QQ 桥接 | OneBot v11 标准协议，后端是 docker 容器 **SnowLuma**（HTTP 3001 发消息，HTTP 上报到 8090） |
| 部署形态 | 云服务器跑 bot（systemd user service）；家里电脑跑 `whale-relay.py`（SSH 反向隧道 + 干重活） |
| 数据在哪 | 全部在 `data/` 下的 json / sqlite，**没有外部存储**，拷贝 `data/` 即完整备份 |
| 规模 | 记忆 772 条节点 / 2143 条突触；曲库 35 首；自测 123 项 |

---

## 1. 运行时拓扑（三层，必须一起理解）

```
┌─────────────────────┐         ┌──────────────────────────────┐
│  手机 QQ 客户端      │         │  家里那台电脑（Windows）      │
│  （主人 / 朋友）      │         │  whale-relay.py              │
└──────────┬──────────┘         │  · 保持 SSH 反向隧道          │
           │ QQ 协议             │  · 领活/交活（轮询）          │
           ▼                     │  · 唱歌：B站搜索→下载→demucs  │
┌─────────────────────┐         │    人声分离→RVC 换声→切片     │
│ docker: snowluma    │         │  · 看视频：抓帧 + ASR         │
│ OneBot v11 后端      │         └──────────▲───────────────────┘
│ · HTTP API :3001    │                    │ ssh -N -L 8091:127.0.0.1:8090
│ · 上报 → :8090      │                    │ （隧道，服务器主动可达家里）
└──────────┬──────────┘                    │
           │ POST http://<server>:8090/    │
           ▼                               │
┌──────────────────────────────────────────┴────────────────┐
│  云服务器（Ubuntu 22.04，2G 内存）                          │
│  whale-bot.service（systemd --user）                       │
│  bot.py：aiohttp 收上报 → 处理 → 调 DeepSeek → 调 3001 发送  │
│  数据：data/*.json + data/neuron.db + data/songs/          │
└────────────────────────────────────────────────────────────┘
```

**为什么这么分**：

- **QQ 协议不由我们实现**，交给 SnowLuma（OneBot 实现），我们只对接 HTTP：链路短、日志清楚。
- **重活放家里**：B 站对机房 IP 有风控、RVC/demucs 需要好显卡；服务器只有 2G 内存，跑不动。
- 家里电脑通过 **SSH 反向端口转发**（`-L 8091:127.0.0.1:8090`）把服务器的 8090 映射到本地，
  由此服务器能“反向”访问家里的服务（不需要公网 IP、不用内网穿透）。

---

## 2. 一条消息的完整旅程

```
QQ 用户发消息
  → SnowLuma 收到 → POST http://<server>:8090/（OneBot v11 事件）
  → bot.py 的 web handler（_on_event / _handle_message）
      ├─ 解析 cid：私聊 "u<QQ>"，群聊 "g<群号>"
      ├─ 白名单判定 is_owner / is_friend：都不是 → 静默 return
      ├─ 斜杠指令？ → handle_command()（/指令、/记忆、/好感度、/事件、/设关系…）
      ├─ 表情包判定（只发表情包 → 走 _reply_to_sticker；表情包+文字 → 本轮不回表情包）
      ├─ 自动回复文本判定（含「[自动回复]」→ 整条不理）
      └─ reply(cid, text, …)   ← 主干
            ├─ 记录 _last_active / _topic_gap / last_incoming（行动台账）
            ├─ 勿扰、情绪（mood）、生闷气忽略（最多 3 条/40 分钟，急事必回）
            ├─ 规则抽取（rules.extract：安静时段 / 话题黑名单 / 定时 / 别发表情包）
            ├─ build_messages()：system 提示词 + 人设 + 关系块 + 记忆召回 + 事实 + 历史
            ├─ llm.chat()：DeepSeek，流式非必需
            ├─ 后处理流水线（顺序很重要，见 §4）
            └─ send() → OneBot HTTP 3001 → QQ
```

---

## 3. 模块地图

### 3.1 主干

| 文件 | 职责 | 备注 |
| --- | --- | --- |
| `bot.py` | 一切主干：HTTP 收事件、指令分发、`reply()`、`build_messages()`、主动消息、定时循环、隐私过滤 | **约 6200 行，改动前先读 §4 的不变量** |
| `config.py` | 所有配置与阈值，全部从 `.env` 读，带回退默认值 | 新参数加在这里，不要在业务代码里写魔法数 |
| `llm.py` | DeepSeek 调用封装（重试、超时、消息裁剪） | 单价敏感：这里控制了 max_tokens |
| `onebot.py` | OneBot v11 客户端：发私聊/群聊、CQ 码拼装、媒体消息构造 | 只做协议，不含业务 |
| `persona.md` / `persona_friend.md` | 人设（主人口吻 / 朋友口吻） | 改完**重启即生效**，不需要改代码 |

### 3.2 记忆系统（最复杂，见 `docs/DATA-MODEL.md`）

| 文件 | 职责 |
| --- | --- |
| `neuron.py` | 神经元记忆：节点 + 突触、扩散激活检索、FSRS 遗忘曲线、巩固、复习调度、embedding |
| `memory.py` | 短期窗口 + 摘要压缩 + 归档（老机制，仍在使用，与 neuron 并存） |
| `neuron_import.py` | 从 `memory.json` 批量抽取原子事实导入神经元（一次性迁移工具） |
| `neuron_shadow.py` | 影子评测：用真实对话比对「神经元召回」vs「随机抽样」的相关度 |
| `neuron_view.py` / `neuron_edit.py` | 3D 记忆云可视化 / 可编辑版（浏览器打开） |
| `neuron_apply_patch.py` | 在服务器端应用编辑补丁（只传增量，不覆盖整库） |

### 3.3 关系与情绪

| 文件 | 职责 |
| --- | --- |
| `affection.py` | 好感度 0–100、5 档、每日上限 6 分、衰减、门禁（唱歌 40 / 画画 60）、行为分层与后处理、吃醋 |
| `mood.py` | 瞬时情绪 −3…+3：被凶/被哄/被夸/陪唱；生闷气（条件：情绪低且关系近） |
| `rules.py` | 自然语言 → 结构化规则：安静时段、话题黑名单（含 45 分钟开放窗口）、一次性/每天/工作日定时、`no_sticker`、歧义反问 |
| `remind.py` | 定时队列：`nearest_at`（取最近未来时刻）、`repeat`、`due(grace=600)`、续排 |

### 3.4 多模态

| 文件 | 职责 |
| --- | --- |
| `sing.py` | 唱歌流水线（**在家里电脑执行**）：B 站搜索 → 打分选歌 → 下载 → demucs 分离 → 音高判定 → RVC 换声 → 切片 |
| `songlib.py` | 曲库：按歌名存已做好的切片，点歌先查曲库（家里电脑关着也能放） |
| `voice.py` | TTS 与音频处理（WSOLA 变速不变调等） |
| `media.py` | 视频/图片理解：抽帧、字幕、URL 识别 |
| `imagegen.py` | 画图 |
| `stickers.py` | 表情包库（按情绪标签选图） |
| `home_agent.py` | **家里电脑侧的 worker**：轮询领活、执行、交活；`CODE_VERSION` 版本握手 |
| `whale-relay.py` | 家里电脑入口（在 `tools/` 里）：维持隧道 + 循环领活 + 交活重试 + 结果落盘补交 |

### 3.5 运维与自检

| 文件 | 职责 |
| --- | --- |
| `selfcheck.py` | 离线自测 **123 项**，不连 QQ、不花 token；改完必须先跑 |
| `sysinfo.py` | 服务器自身状态（内存/磁盘/容器/中继在线） |
| `notify.py` | 邮件通知（掉线时发二维码等） |
| `evolve.py` | 性格快照（只对主人；朋友不参与“她的成长”） |
| `build_library.py` | 批量预热曲库 |
| `build_docs.py` | 把 md 转成自带样式的 HTML（客户端 markdown 预览有 bug，用这个绕开） |

---

## 4. 六条不变量（**动代码前必读**）

这六条都是踩过事故之后定下来的，破坏任何一条都会立刻出事：

1. **模型只负责抽取结构，代码负责算时间、判歧义、执行拦截。**
   模型说“好”没有约束力；它给的 `today/tomorrow` 不可信（它不知道现在几点）。
   例：`rules.time_question()` / `past_question()` 都由代码判，不信模型的 `ambiguous`。

2. **同一个要求，后说的覆盖先说的。**
   安静时段、话题、`no_sticker`、定时——用户改主意是常态；
   “永远记着第一条”是 bug，不是严谨。

3. **凡是“给主人自查/内部状态”的输出，入口必须判身份，发送前再过滤一次。**
   事故背景：朋友问“不记得了吗”，她开始倾倒记忆摘要，末尾还带了「爸爸：」——
   泄露了主人的存在。现在 `_sanitize_for_friend()` 是第二道闸。

4. **缓存会绕过所有新逻辑。**
   凡是“先查本地缓存命中就直接用”的路径（曲库、预加载缓冲、embedding 缓存），
   必须用**同一个校验函数**，否则改了选择逻辑也白改。

5. **“生成成功”≠“用户收到”。**
   收尾那一步（发送、补发）必须能独立失败、且失败要吼出来，
   不能只写一行 WARNING（曾经歌做好了、补发崩在 `NameError` 上，用户等到天亮）。

6. **她说的“我做过什么”，必须有台账。**
   `_verify_claims()` + `_actions` 记录真实发过的动作；
   台账里没有的就删掉那句话——模型会顺着历史编“我刚唱了”。

---

## 5. 后处理流水线（`reply()` 里的顺序，别乱改）

```
模型输出
 → strip_actions()      去掉 *动作描写*
 → strip_markers()      去掉 [[贴图:…]] [[唱:…]] [[查:…]] 等标记
 → clean_output()       去 markdown、去 CQ 码残留、长度控制
 → ensure_bubbles()     拆成 1–3 条短消息（像真人打字）
 → affection.postprocess()   按好感度加/减语气（Lv1 删“抱抱”、句尾语气词）
 → _verify_claims()     声称做过的事必须对得上台账
 → 话题黑名单检查        踩到就重说一次，仍踩则删句
 → _sanitize_for_friend()   朋友路径：抹掉“爸爸”和内部细节
 → send() → OneBot
```

顺序错一步就会互相打架（例如先 sanitize 再 postprocess，会被 postprocess 重新加回“抱抱”）。

---

## 6. 后台循环（常驻任务）

| 时间/周期 | 任务 | 代码位置 |
| --- | --- | --- |
| 每 10 秒 | 定时消息检查（`remind.due`），到点直接发，不走 `reply()` 的阀门 | `_remind_loop` |
| 每 30 秒 | 补话检查（主人不回就再补一句，最多 N 次） | `_followup_loop` |
| 90–180 分钟随机 | 主动找主人说话（话题角度随机，受规则闸门约束） | `_proactive_loop` |
| 同上循环内 | 主动找朋友（每天上限、5 天没回则静音） | `_maybe_say_to_friends` |
| 每天 04:00 | 记忆维护：`decay` 重算 R + `consolidate` 巩固 + `replay` 复习（挑 0.15≤R≤0.80）+ 生成“梦” | `_neuron_maint_loop` |
| 每天 05:00 | 影子评测 20 样本，结果 append 到 `data/shadow_daily.jsonl` | `_neuron_shadow_loop` |

---

## 7. 数据文件（都在 `data/`，随工程一起备份）

| 文件 | 内容 | 谁写 |
| --- | --- | --- |
| `memory.json` | 对话窗口 + 摘要 + 归档（按 cid 隔离） | `memory.py` |
| `neuron.db` | sqlite：`neuron` 节点表 + `synapse` 突触表 | `neuron.py` |
| `emb_cache.json` | embedding 磁盘缓存（文本 → 1024 维向量） | `neuron.py` |
| `affection.json` | 好感度、事件、衰减、门禁冷却 | `affection.py` |
| `mood.json` | 瞬时情绪、忽略计数 | `mood.py` |
| `rules.json` | 规则：安静时段/话题/定时/no_sticker | `rules.py` |
| `reminders.json` | 定时队列 | `remind.py` |
| `contacts.json` | 称呼、关系（lover/family/…）、性别、勿扰、`last_incoming` | `bot.py` |
| `state.json` | 跨重启状态：看视频记录、行动台账、最后发的歌 | `bot.py` |
| `songs/index.json` + `songs/*.mp3` | 曲库索引 + 切片音频 | `songlib.py` |
| `announcements.jsonl` | 公告历史 | `bot.py` |
| `shadow_daily.jsonl` | 每日记忆评测 | `neuron_shadow.py` |

**字段与算法细节见 `docs/DATA-MODEL.md`。**

---

## 8. 怎么跑起来（30 分钟）

```bash
# 1) Python 依赖
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2) 配置
cp .env.example .env        # 然后填 4 个 key：DEEPSEEK / DASHSCOPE / OneBot token / 主人 QQ

# 3) 起 QQ 桥接（OneBot v11 后端）
docker run -d --name snowluma -p 127.0.0.1:3001:3001 \
  -e ONEBOT_ACCESS_TOKEN=<同 .env> motricseven7/snowluma:latest
#    首次需要扫码登录 QQ（详见 deploy/snowluma.md）

# 4) 自测（不连 QQ、不花钱）
python selfcheck.py           # 期望：共 123 项，通过 123 项

# 5) 启动
python bot.py                 # 或 systemctl --user start whale-bot（详见 docs/OPERATIONS.md）
```

**没有家里中继也能跑**：唱歌会提示“家里那台电脑没连着”，其余功能（聊天/记忆/好感/规则/看图）全正常。

---

## 9. 测试与验收标准

- `python selfcheck.py` —— **唯一硬门槛**，123 项全过才算改完。
  它覆盖：纯函数、指令解析、隐私隔离（朋友看不到“爸爸”）、贴图逻辑、自检模块 import、
  规则隔离（改一条不冲掉另一条）等。
- 手动冒烟：
  - 主人：`/指令` `/记忆` `/好感度` `/事件` `/设关系` 各发一次
  - 朋友（白名单内）：确认回的是“朋友口吻”，且**看不到**“爸爸”两个字
  - 三人以外：发消息应**完全无回应**
- 记忆系统改动额外跑：`python neuron_shadow.py`，看提升是否仍 > 15%

---

## 10. 已知问题 / 优化路线（按优先级）

### P0 · 安全与隐私（最该先做）

1. **能力面不可穷举**：她能执行 shell（唱歌/看视频要调 ffmpeg/demucs）。
   目前靠“代码里不写危险命令”约束，没有白名单。
   → 参考做法：把可执行动作收敛成一个 `tools/` 脚本集合 + 显式授权表。
2. **`data/` 没有任何加密**：服务器被拿就走全部私聊。
   → 建议：静态加密或至少每日加密备份。
3. **密钥只在 `.env`**：无轮换机制。
4. **朋友的隐私边界只靠代码约定**：`_sanitize_for_friend()` 是黑名单式过滤，容易漏。

### P1 · 可靠性

5. **单进程无守护**：`systemd` 会拉起，但进程内异常只记日志。
   → 加健康检查端点 + 看门狗。
6. **预加载缓冲在内存里**：重启即丢（已有“丢了就重做”的兜底，但会慢一次）。
7. **`bot.py` 6200 行**：`reply()` 是巨型函数。→ 建议逐步拆成 `handlers/`、`pipeline/`。
8. **日志是纯文本**：查问题时靠 grep。→ 结构化字段（cid/kind/耗时）。

### P2 · 体验

9. 记忆检索**没有时间维度**参与排序（“上周”和“去年”权重一样）。
10. 好感度 4 级和 5 级的**措辞分不开**（只能靠主动频率、吃醋等别的杠杆）。
11. 曲库匹配仍是字符串打分，多音字/同义歌名会漏。

### P3 · 扩展

12. 记忆可视化只支持本地打开（服务器 2G，不适合在服务端渲染网页转图）。
13. 多人群聊支持很弱（目前只认 @）。
14. 没有 Web 管理面板（所有管理都靠 QQ 指令）。

---

## 11. 你可以改什么、不能改什么

**可以**：任何代码、提示词、阈值、文档。改完跑 `selfcheck.py`。

**不要**：

- 不要把 `.env`、`data/`、任何真实 QQ 号/密钥提交到仓库（见 `SECURITY.md`）
- 不要绕过 `sanitize_for_friend()`
- 不要用“提示词里加一句”代替代码闸门（见 §4 第 1 条）
- 不要在 `reply()` 里插代码而不管后处理顺序（见 §5）

---

## 12. 相关文档

| 文档 | 内容 |
| --- | --- |
| `docs/ARCHITECTURE.md` | 架构与数据流的更细版本（含模块依赖、时序） |
| `docs/DATA-MODEL.md` | 所有数据结构的字段、算法公式、阈值 |
| `docs/OPERATIONS.md` | 部署、systemd、docker、家里中继、排障手册 |
| `docs/LESSONS.md` | 踩坑记录（脱敏精选，最实用的部分） |
| `CONTRIBUTING.md` | 改代码的流程与提交规范 |
| `SECURITY.md` | 密钥、隐私边界、绝不能提交的东西 |
