# 架构（ARCHITECTURE）

> 本文回答三个问题：系统由什么组成、数据怎么流、为什么这样切分。
> 快速总览看 `HANDOVER.md`；字段与公式看 `DATA-MODEL.md`。

---

## 1. 为什么是「单进程 + HTTP 双通道」

早期也考虑过 NoneBot2 / Koishi 这类框架。结论是**不用**：

- 场景是「一个人 + 几个朋友」的私聊，插件体系是纯负担；
- OneBot v11 的 **HTTP 上报 + HTTP 调用**已经够用：链路短、日志一行一条、断线交给后端重连；
- 全项目必需依赖只有 `aiohttp`（+ 可选 `httpx`），2G 服务器装得下。

代价：`bot.py` 越长越难维护（已 6200 行）。这是当前最大的技术债，见 §6。

---

## 2. 进程内结构

```
WhaleBot
├── HTTP 层（aiohttp）
│   ├── POST /            OneBot 事件上报（唯一入口）
│   ├── POST /jobs/*      家里电脑领活 / 交活（带 token 与版本握手）
│   └── GET  /health      健康检查
├── 分发层
│   ├── _handle_message() 白名单判定 → 指令分流 → reply()
│   └── handle_command()  斜杠指令（表驱动：COMMANDS）
├── 生成层
│   ├── build_messages()  拼 system 提示词（人设/关系/记忆/事实/历史）
│   ├── llm.chat()        DeepSeek
│   ├── action_protocol   纯解析动作并清理正文
│   ├── action_runtime    歌单/贴图/自查权限与 trace
│   └── 后处理流水线       见 HANDOVER §5
├── 子系统（每个一个模块）
│   ├── memory + neuron   记忆
│   ├── affection + mood  关系与情绪
│   ├── rules + remind    约束与定时
│   ├── sing + songlib    唱歌（派活给家里）
│   ├── media + imagegen  多模态
│   └── voice + stickers  语音与贴图
└── 后台循环
    ├── _remind_loop        10s
    ├── _followup_loop      30s
    ├── _proactive_loop     90–180 分钟
    ├── _neuron_maint_loop  每天 04:00
    └── _neuron_shadow_loop 每天 05:00
```

**依赖方向是单向的**：`bot.py` 依赖所有模块；模块之间尽量互不依赖
（例外：`songlib` 读 `config`；`neuron` 读 `config`；`rules`/`remind` 互不依赖）。

---

## 3. 两条消息路径的分叉点

```
收到事件
  ├── 私聊 & 白名单外 → return（静默，连“不认识你”都不回）
  ├── 群里 & 未 @ 她 → return
  ├── 斜杠指令 → handle_command()（不进模型）
  └── 普通消息 → reply()
        ├── 只发表情包 → _reply_to_sticker()（现编一句问句，不背固定台词）
        └── 文字 → build_messages() → llm → 后处理 → send
```

**指令与对话必须分开**：指令走确定性代码（不该花 token、也不该被情绪/勿扰影响）；
对话才进模型。

---

## 4. 主动消息：两条独立路径

| | 主人 | 朋友 |
| --- | --- | --- |
| 触发 | `_proactive_loop`，90–180 分钟随机 | 同一循环里顺带调 `_maybe_say_to_friends` |
| 闸门 | 勿扰、作息规则、静默阈值（120 分钟） | 勿扰、作息规则、每天上限 5 次、**5 天没回就静音** |
| 话题 | 固定角度池（PROACTIVE_ANGLES），可拼接“我刷到什么” | 同理但更保守 |
| 补话 | 发完登记 `_pending`，30 秒循环里检查，最多补 N 次 | 不补 |
| 频率缩放 | **不缩放**（用户手动调过，不能被好感度系数改） | 按好感等级缩放（Lv1 1.8× → Lv5 0.7×） |

**为什么朋友不补话**：给朋友连发两条是打扰；补话只对主人有效。

---

## 5. 家里电脑（relay）的协议

```
服务器                                  家里电脑（whale-relay.py）
  ├── GET  /jobs/poll?v=<版本>         ←── 每 1.5 秒轮询（X-Home-Token）
  │       返回 {"job": {"id","kind","query","segment",…}}
  ├── POST /jobs/result                 ←── 做完交作业（最多重试 40×3s）
  │       返回 {"dup": true}（幂等：同一 job 只处理一次）
  └── 版本握手：家里 CODE_VERSION < config.HOME_MIN_VERSION
         → 服务器回「家里那个黑窗口还开着旧版程序」
```

设计要点：

1. **隧道由家里发起**（`ssh -N -L 8091:127.0.0.1:8090`），服务器不需要内网穿透。
2. **结果落盘 + 启动补交**：`%TEMP%\whale_pending_report\<job_id>.json`，
   交成功才删；程序崩了下次启动自动补交。
3. **交作业幂等**：服务器用 `delivered` 集合去重，重试不会把同一首歌发两遍。
4. **断线重做**：处理中抛连接类异常 → 等隧道恢复 → 重做这一段（最多 3 次）。

---

## 6. 已知结构问题（优化入口）

### 6.1 `bot.py` 是巨石

- `reply()` 单函数覆盖 400+ 行，承担：权限、勿扰、情绪、规则、记忆召回、生成、
  后处理、发送、记账。**建议拆分顺序**：
  1. `pipeline/` —— 后处理流水线抽成 `list[Step]`（现在是一串 if，顺序靠自觉）
  2. `handlers/` —— 指令各自一个函数（现在是 `handle_command` 里 30 个 `elif`）
  3. `proactive.py` —— 两条主动路径抽出去
- 拆分时**不要改行为**，先补 selfcheck 覆盖，再动结构。

### 6.2 状态存储是“文件 + 全量写”

每个模块都是 `读 json → 改 → 写 json`。数据小的时候没问题，
但 `contacts.json` 每条消息都可能写（`last_incoming`），已经加了 1 小时节流。
**如果要支撑更多朋友，应该换成 sqlite**（表结构见 DATA-MODEL §7）。

### 6.3 没有依赖注入，全局单例多

`HOME_VERSION`、`_song_session`、`_pending` 等是模块级/实例级全局。
写测试时不好替换。**建议**：`WhaleBot.__init__` 里注入，测试用假对象。

---

## 7. 时序：一次唱歌

```
主人：「唱首 修炼爱情」
  bot.reply()
    ├─ wants_sing() 命中 → asked_song = pick_song_name()
    ├─ affection.can(cid,"sing") → 不够则解释（带 6h 冷却）
    ├─ 曲库 songlib.find("修炼爱情") → 命中第 0 段 → 直接发（秒出）
    ├─ 未命中 → _ask_home("sing", query, segment=0)
    │     → 家里 relay 轮询领活 → sing.make_segment()
    │         ├─ B站搜索（20 条）→ rank 打分 → 选歌
    │         ├─ 下载音频 → demucs 分离人声
    │         ├─ detect_pitch_shift()（往下最多 −2 半音）
    │         ├─ RVC 换声（cover.py）→ 切片 → base64
    │     → 交作业（幂等）→ 服务器存 data/singing/ → _send_audio()
    ├─ 成功后：mark_action("sing")、记曲库、建 _song_session（next=1）
    └─ 说「这是第 1/5 段～还要听吗」
主人：「继续」 → _sing_continue() 取预加载缓冲 / 曲库 / 重做
```

**预加载**：发第 N 段的同时后台开始做第 N+1 段，用户说“继续”才秒出。
预加载只存在内存（`session["buffer"]`），重启即丢 → 已加“丢了就重做”兜底。

---

## 8. 时序：一次“她答应记住要求”

```
对方：「以后九点前别给我发消息」
  reply()
    ├─ rules.wants_extract() → 命中提示 → rules.extract()（模型抽 JSON）
    ├─ 代码逐字段校验（_valid_hm、days 0~6、min_gap ≤240）
    ├─ 代码判歧义（time_question / past_question）→ 有歧义就反问 + 存 pending
    ├─ 无歧义 → _apply_parsed()：rules.save() + remind 排程
    └─ 她复述（提示词里回灌 rules.prompt_block，所以她复述不会说错）
对方：「对的」→ 走确认流程 → _apply_parsed() 落地（两个入口共用同一方法）
之后每次主动发送前：rules.allows_proactive(cid, now) 不通过就不发
```

**关键**：约束不是“她记住了”，是**代码每次发送前都查一遍**。

---

## 9. 隐私边界（三层）

```
第 1 层  入口判身份：is_owner(cid) 才允许 [[查:…]] 这类自查
第 2 层  生成时不给：朋友路径不塞梦、不塞主人相关事实
第 3 层  发送前过滤：_sanitize_for_friend() 抹掉“爸爸”和内部字段
```

三层缺一层都会漏（第一版只有第 2 层，事故见 `LESSONS.md`）。
