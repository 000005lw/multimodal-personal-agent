# 数据模型与算法（DATA-MODEL）

> 所有状态都在 `data/` 下的 json / sqlite 里。本文给字段、公式、阈值。
> 改这些数值之前，先读 `docs/HANDOVER.md` §4 的六条不变量。

---

## 1. 记忆：`neuron.db`（核心）

两张表。

```sql
CREATE TABLE neuron (
  id         INTEGER PRIMARY KEY,
  cid        TEXT,      -- 归属人 "u<QQ>"，记忆按人隔离（硬约束，绝不跨人）
  kind       TEXT,      -- fact | prefer | mood | person | event
  text       TEXT,      -- 原子事实，一句话（不是原话）
  vec        BLOB,      -- float32 数组，1024 维（text-embedding-v3）
  strength   REAL,      -- 长期强度 0..1，参与初始激活
  valence    REAL,      -- 情绪价 −1..1（未深度使用）
  created    REAL,      -- 入库时间戳
  last_hit   REAL,      -- 上次被想起
  hits       INTEGER,   -- 被想起次数
  stability  REAL,      -- FSRS 稳定性 S（天）
  reps       INTEGER,   -- 复习次数
  lapses     INTEGER    -- 想不起来次数
);

CREATE TABLE synapse (
  a    INTEGER,   -- 节点 id
  b    INTEGER,
  w    REAL,      -- 权重 0..1
  kind TEXT       -- cooc（共现）等
);
```

### 1.1 遗忘曲线（FSRS）

```
R(t) = (1 + F·t/S)^(-0.5)        F = 19/81
t = 距上次想起的天数，S = 稳定性（天），R = 现在还能想起来的概率
t = S → R = 0.90；t ≈ 12.8S → R = 0.50
```

初始 S 按类型（`STABILITY`）：

| kind | 含义 | 初始 S |
| --- | --- | --- |
| person | 人物设定 | 6.0 天 |
| prefer | 偏好 | 5.0 天 |
| fact | 事实 | 3.0 天 |
| event | 事件 | 1.2 天 |
| mood | 情绪 | 0.4 天 |

成功回忆（`hit`）：

```
S' = min(365, max(0.2, S × (1 + 0.9×(1−R)) × 1.15))
strength' = min(1, R + (1−R)×0.8)
```

想不起来（`lapse`）：

```
S' = max(0.2, S × 0.3)
strength' = 0.25
```

### 1.2 检索：扩散激活

```python
q = embed(text)                       # 1024 维
对每条记忆: score = cosine(q, vec)
   if score > 0.30: act[id] = score * (0.30 + 0.70 * strength)
seeds = 最像的 top4（≥ 最高分 × 0.85）
扩散 hops 次: act[邻居] += base * synapse.w * decay
截断: act ≥ max(act) × 0.78 且 ≥ 0.36
返回 top5 → 塞进提示词 → 命中项 hit()
```

三个关键点：

- **× strength**：快忘光的记忆，即使字面很像也不该跳出来。
- **只从 top4 扩散**：从全库扩散会两跳传到完全不相关的地方。
- **双门槛**：相对 0.78 保证“别凑数”，绝对 0.36 保证“真没有就返回空”。

### 1.3 每日维护（04:00）

```
decay()        重算所有 R
consolidate(threshold=0.55)   反复激活的短期记忆 → 长期
replay()       挑 0.15 ≤ R ≤ 0.80（快忘但还有救）复习 8 条，复习完 hit()
               → 揉成一句“昨晚的梦”存 data/neuron_dream.json
```

### 1.4 embedding

- 主：百炼 `text-embedding-v3`，1024 维，磁盘缓存 `data/emb_cache.json`
- 兜底：`zlib.crc32` 的字符 n-gram 哈希（断网时；效果差很多）
- 维度不一致时**自动重算并回写**（换模型后自愈）

---

## 2. 短期记忆：`memory.json`

```json
{
  "conversations": {
    "u<QQ>": {
      "messages": [{"role": "user|assistant", "content": "…"}],
      "summary": "压缩后的长期摘要",
      "archived": "更早的归档"
    }
  }
}
```

三段式：**近期原话窗口 → 摘要 → 归档**。窗口满了触发压缩（调模型摘要）。
与 neuron 并存：窗口负责“刚才说了什么”，neuron 负责“她记得什么”。

---

## 3. 好感度：`affection.json`

```json
{"u<QQ>": {"score": 62.0, "level": 4, "last_up": 1762…,
           "events": [{"ts": 1762…, "delta": 2, "why": "今天第一次说话"}],
           "streak": 3, "day": "2026-11-09", "gain_today": 4.0,
           "locked_at": {"sing": 1762…}, "missed": 4.5}}
```

| 规则 | 值 |
| --- | --- |
| 分值 | 0–100；主人**恒定 100**（`is_pinned`，全部加分/衰减/惩罚跳过） |
| 等级 | 1 刚认识 0 / 2 有点熟 20 / 3 亲近 40 / 4 交心 60 / 5 粘人 80 |
| 每日加分上限 | **6**（`DAILY_GAIN_CAP`）；`/改好感` 走 `force=True` 不受限 |
| 门禁 | 唱歌/看视频 **40**，画画 **60** |
| 衰减 | 3 天不说话开始，**1.5 分/天**，单次最多 −9 |
| 被凶 | −3（词表：滚/烦死/废物/讨厌你/没用/不想理你/闭嘴/傻逼/去死/妈的/滚蛋/垃圾） |
| 被夸 | +1（每天最多 2 次） |
| 聊天 | 首次 +2；每 10 句 +1；连续天数 ≥3 每天 +1；陪唱/看视频 +2 |

**行为分层** `BEHAVIOR[1..5] = {tone, avoid[], example, clingy, proactive, call}`：
提示词里给 tone + 必须避开的说法 + 一条示例；**真正拉开差异的是
`affection.postprocess()`**（Lv1 删“抱抱/亲亲/想你”、句尾语气词、最多两行）。

---

## 4. 情绪：`mood.json`

```json
{"u<QQ>": {"score": -2.0, "ts": 1762…, "ignored": 2, "ignore_start": 1762…,
           "sulk_at": 1762…, "last_decay": 1762…}}
```

| 事件 | 变化 |
| --- | --- |
| 被凶 | −2 |
| 被哄 | +1.5 |
| 被夸 | +1 |
| 陪她唱完一首 | +1 |
| 24 小时 | 自愈回 0（线性回落） |

**生闷气**（`is_sulking`）：`mood ≤ −2` **且** 好感 ≥ 40（关系够近才闹）。
闹的时候最多忽略 3 条 / 40 分钟；**带“急/难受/在吗/救命”等词必回**。

---

## 5. 规则：`rules.json`

```json
{"u<QQ>": {
  "quiet": [{"start": "00:00", "end": "09:20", "days": "all|0-6"}],
  "no_proactive": false,
  "min_gap_min": 60,
  "topics": ["吃了没", "吃饭", "吃了吗"],
  "no_sticker": true,
  "note": "他当时的原话",
  "pending": {"summary": "…", "ambiguous": true},
  "topic_seen": {"吃饭": 1762…},
  "hits": 0, "violations": 0, "updated": 1762…
}}
```

| 类型 | 拦在哪 | 拦完怎么办 |
| --- | --- | --- |
| 安静时段 | **发送前** | 不发，等下个窗口 |
| 话题黑名单 | **生成后** | 让她重说一次；仍踩则删句 |
| `no_sticker` | 发送前 | 丢弃 `[[贴图:…]]` 标记 + 跳过随机补图 |

话题的 **45 分钟开放窗口**（`TOPIC_OPEN_MIN`）：他自己提到的话题，45 分钟内她可以顺着聊；
**只有“他提到”能续期**，她自己反复提不能续期（否则她能靠自言自语永久占住）。

`no_sticker` 是**三态**语义：字段不存在/None = 他这次没提，别动；
`True` = 禁；`False` = 解禁（他说“可以发了”）。

---

## 6. 定时：`reminders.json`

```json
[{"id": "…", "cid": "u<QQ>", "at": 1762…, "note": "叫你起床",
  "raw": "每天7点半叫我起床", "repeat": "once|daily|weekdays",
  "done": false, "created": 1762…}]
```

- `nearest_at("11:30")` 在 **今天 11:30 / 今天 23:30 / 明天 11:30 / 明天 23:30** 里取最近未来点；
  `hh > 12` 时不加 +12h 候选（否则 23:44 说“23:30”会被算成明天上午）。
- 歧义反问（**代码判**，不信模型）：
  - `time_question()`：上午还是晚上？触发条件——时间点靠 +12h 翻出来且 > 6 小时
  - `past_question()`：今天这个点已过去 <12 小时且最近点 >6 小时 → “是明天吗”
- `due(grace=600)`：超过 10 分钟没发就作废（不隔天补发）
- 发送走 `_fire_reminder()`，**不走 `reply()`**（定时最高优先级，不该被勿扰/生闷气拦掉）
- 发完按 `repeat` 续排：`daily` 排明天同点，`weekdays` 跳过周末

---

## 7. 联系人：`contacts.json`

```json
{"<QQ>": {
  "name": "称呼",                    // 她怎么叫他（/叫我 改）
  "relation": "女朋友",              // 自由文本：女朋友/男朋友/妹妹/老大/朋友…
  "gender": "f",                     // f | m | ""（空 = 不预设）
  "awaiting": false,                 // 是否在等他说称呼
  "dnd": false,                      // 勿扰
  "unreachable": false,              // 发不出去
  "poke": {"date": "2026-11-09", "count": 1, "next": 1762…},
  "last_incoming": 1762…,            // 他本人最后说话（>=5 天不再主动找他）
  "locked_at": {"sing": 1762…}        // 门禁回绝冷却
}}
```

**关系分类**（`_kind_of_label`）：`lover`（女朋友/男朋友/恋人/对象/老公/老婆）、
`family`（妹妹/姐姐/哥哥/弟弟/女儿/儿子）、`friend`（朋友/基友/闺蜜）、
`other`（其余，走 `_SOCIAL_PRESETS`：老师/同学/同事/上司/邻居/长辈/网友/学长…）。
性别优先取显式（末尾“她/他”），否则从词里猜（女/妹/姐/婆 → f；男/哥/弟/公 → m），
**猜不出就全程用“对方”，并禁止她替对方定性别**。

---

## 8. 跨重启状态：`state.json`

```json
{"watch_asked": {}, "watched_urls": {}, "watch_plan": {},
 "qzone_plan": {}, "qzone_recent": [], "media_seen": {},
 "last_song": {"<QQ>": {"query": "…", "title": "…", "index": 0, "total": 5, "ts": 1762…}},
 "actions":   {"<QQ>": {"sing": 1762…, "video": 1762…, "sticker": 1762…}}}
```

- `last_song`：让「继续」在**重启后**也能接上（1 小时内有效）。
- `actions`：行动台账。她说“我刚唱了”之前，代码查这里；没有就删那句话。

---

## 9. 曲库：`songs/index.json`

```json
{"修炼爱情": {"query": "修炼爱情",
              "title": "在百万豪装录音棚大声听林俊杰《修炼爱情》【Hi-res】",
              "segments": {"0": {"file": "a1b2.mp3", "ext": "mp3"}, "1": {…}},
              "total": 5, "ts": 1762…,
              "file": "a1b2.mp3", "ext": "mp3"}}
```

选歌（`sing.rank/_score`）的硬约束，全部踩过坑之后加的：

| 规则 | 原因 |
| --- | --- |
| 标题必须**含歌名**（≤4 字要求“独立出现”） | 否则「简评蔷薇龙补强」会被当成《蔷薇》 |
| 命中 `HARD_BAD` 直接否 | 歌单/循环/合集/倍速/升调/dj/伴奏/KTV/简评/攻略/原神/铃声… |
| 时长 < 60 秒或 > 480 秒直接否 | 切片、43 分钟“循环歌单” |
| 2.5–6 分钟 +4，1.5–2.5 分钟 +3 | 优先完整版（1:15 的短片不该赢过 4:00 的完整版） |
| 知道歌手时，只认标题里真出现歌手 | 否则搜“铁皮软糖 蔷薇”会返回萧亚轩 |
| 变调：往下最多 −2 半音（`DOWN_LIMIT`） | 女声被硬压 6 个半音会变怪声 |

---

## 10. 阈值速查

| 参数 | 值 | 位置 |
| --- | --- | --- |
| 每日好感加分上限 | 6.0 | `affection.DAILY_GAIN_CAP` |
| 好感衰减 | 1.5/天，单次 ≤9 | `affection.DECAY_PER_DAY` |
| 唱歌门禁 / 画画门禁 | 40 / 60 | `affection.GATES` |
| 生闷气阈值 | mood ≤ −2 且好感 ≥ 40 | `mood.is_sulking` |
| 忽略上限 | 3 条 / 40 分钟 | `mood` |
| 话题开放窗口 | 45 分钟 | `rules.TOPIC_OPEN_MIN` |
| 定时宽限 | 600 秒 | `remind.due` |
| 定时检查周期 | 10 秒 | `bot._remind_loop` |
| 朋友静音阈值 | 5 天没回 | `config.FRIEND_SILENT_STOP_DAYS` |
| 话题重置（隔久换话题） | 45 分钟 | `config.TOPIC_RESET_MIN` |
| 记忆检索门槛 | 0.30 / 相对 0.78 / 绝对 0.36 | `neuron.activate` |
| 记忆维护 | 04:00 | `bot._neuron_maint_loop` |
| 影子评测 | 05:00，20 样本 | `bot._neuron_shadow_loop` |
