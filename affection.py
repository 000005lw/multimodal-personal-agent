"""好感度：聊得越多越熟，熟了才肯给你更多东西。

设计原则（重要，别推翻）：
1. **基本盘不锁**。说话、语音、表情包、看图、记忆永远能用——
   锁掉这些会让她"变哑巴"，体验直接崩。
2. **只锁"额外劳动"**：唱歌、看视频（40）、画画（60）。
   这些是她要费力气的事，用好感度当门槛说得通。
3. **主人起点高**（62，交心）。规则上线那天不能把已经在用的功能收回去。
4. 好感度是给人看的，**必须能查、能解释**——`/好感度` 要说清"为什么涨"。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import config

PATH = config.DATA_DIR / "affection.json"

# (等级, 分数下限, 名字)
LEVELS = [
    (1, 0, "刚认识"),
    (2, 20, "有点熟"),
    (3, 40, "亲近"),
    (4, 60, "交心"),
    (5, 80, "粘人"),
]

# 功能 → (显示名, 需要的分数)
UNLOCK = {
    "sing": ("唱歌", 40),
    "watch": ("看视频", 40),
    "draw": ("画画", 60),
}

# 永远是基本盘，不参与解锁
FREE = ("语音", "表情包", "看图", "记忆", "聊天")

PRAISE = (
    "可爱", "乖", "好棒", "真棒", "厉害", "喜欢你", "爱你", "聪明",
    "好听", "漂亮", "好看", "心疼", "辛苦了", "谢谢", "想你了", "棒",
)
# 只收"明确在凶她"的说法。像「笨」「别说了」「烦人」这种打情骂俏的
# 常见口吻不收，否则日常开玩笑就掉分，体验很差。
SCOLD = (
    "滚", "烦死", "废物", "讨厌你", "没用", "不想理你",
    "闭嘴", "傻逼", "去死", "妈的", "操你", "滚蛋", "垃圾",
)
SCOLD_DELTA = -3.0

# 每天最多涨多少（防止刷）
# 2026-10-29 用户反馈"涨太快了"，12 → 6。按满速算：
# 0→20 要 3.3 天，→40 要 6.7 天，→60 要 10 天，→100 要 16.7 天
DAILY_GAIN_CAP = 6.0
# 多少天不说话开始掉分
IDLE_DAYS_BEFORE_DECAY = 3
# 掉分速度：用户 2026-10-29 要求"要有惩罚感"，从 0.6 提到 1.5
# 一周不理她 = 3 天之后每天 1.5，掉 6 分；一个月不掉她的耐心就没了
DECAY_PER_DAY = 1.5
DECAY_CAP = 9.0
# 主人永远是满分——用户原话："我是她主人她爸爸，对她的好感度必须是 100 永远 100"
OWNER_PIN = 100.0


def _load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _days_between(a: str, b: str) -> int:
    try:
        ta = time.mktime(time.strptime(a, "%Y-%m-%d"))
        tb = time.mktime(time.strptime(b, "%Y-%m-%d"))
        return int(round((tb - ta) / 86400))
    except Exception:
        return 99


def _new_record(seed: float) -> dict:
    return {
        "score": float(seed),
        "streak": 0,
        "last_day": "",
        "gain_day": "",
        "gain_today": 0.0,
        "msgs_day": "",
        "msgs_today": 0,
        "praise_day": "",
        "praise_today": 0,
        "events": [],
    }


def seed_for(cid: str) -> float:
    """老用户按已有聊天量给个初始分，别让规则一上线所有人归零。"""
    from bot import is_owner  # 延迟导入，避免循环依赖
    if is_owner(cid):
        return 62.0
    try:
        data = json.loads((config.DATA_DIR / "memory.json").read_text(encoding="utf-8"))
        conv = (data.get("conversations") or {}).get(cid) or {}
        n = len(conv.get("messages") or [])
    except Exception:
        n = 0
    return float(min(38, 20 + n // 8))


def record(cid: str) -> dict:
    data = _load()
    rec = data.get(cid)
    if not rec:
        rec = _new_record(seed_for(cid))
        data[cid] = rec
        _save(data)
    if is_pinned(cid) and float(rec.get("score") or 0) != OWNER_PIN:
        rec["score"] = OWNER_PIN
        data[cid] = rec
        _save(data)
    return rec


def is_pinned(cid: str) -> bool:
    """主人永远满分，规则对他不生效。"""
    try:
        from bot import is_owner
        return is_owner(cid)
    except Exception:
        return False


def score(cid: str) -> float:
    if is_pinned(cid):
        return OWNER_PIN
    return float(record(cid).get("score") or 0.0)


def level_of(value: float) -> tuple[int, str, int, float]:
    """返回 (等级, 名字, 本级下限, 下一级门槛)。"""
    lv, name, floor = LEVELS[0]
    for l, need, n in LEVELS:
        if value >= need:
            lv, name, floor = l, n, need
    nxt = 100.0
    for l, need, n in LEVELS:
        if need > value:
            nxt = float(need)
            break
    return lv, name, floor, nxt


def can(cid: str, feature: str) -> tuple[bool, int]:
    """能不能用某个功能。返回 (可以吗, 需要多少分)。"""
    need = UNLOCK.get(feature, ("", 0))[1]
    return score(cid) >= need, need


def add(cid: str, delta: float, why: str, force: bool = False) -> dict:
    """加减分。涨分受每日上限约束，扣分不受限。主人恒定 100，不参与。

    force=True 用于主人手动调整——那是人在改，不该被"每天最多涨 12"卡住。
    """
    if is_pinned(cid):
        return record(cid)
    data = _load()
    rec = data.get(cid) or _new_record(seed_for(cid))
    today = _today()
    if rec.get("gain_day") != today:
        rec["gain_day"] = today
        rec["gain_today"] = 0.0

    if delta > 0 and not force:
        room = max(0.0, DAILY_GAIN_CAP - float(rec.get("gain_today") or 0))
        delta = min(delta, room)
        if delta <= 0:
            data[cid] = rec
            _save(data)
            return rec
        rec["gain_today"] = float(rec.get("gain_today") or 0) + delta

    rec["score"] = round(max(0.0, min(100.0, float(rec.get("score") or 0) + delta)), 1)
    events = rec.get("events") or []
    events.append({"ts": time.strftime("%m-%d %H:%M"), "d": round(delta, 1), "why": why})
    rec["events"] = events[-30:]
    data[cid] = rec
    _save(data)
    return rec


def touch_message(cid: str) -> list[str]:
    """收到一条消息时调用。处理每日首聊、连续天数、每 10 句加分。

    返回给日志看的说明列表（涨了什么）。
    """
    data = _load()
    rec = data.get(cid) or _new_record(seed_for(cid))
    today = _today()
    notes: list[str] = []

    if rec.get("last_day") != today:
        gap = _days_between(rec.get("last_day") or today, today) if rec.get("last_day") else 99
        rec["streak"] = int(rec.get("streak") or 0) + 1 if gap == 1 else 1
        rec["last_day"] = today
        data[cid] = rec
        _save(data)
        add(cid, 2.0, "今天第一次说话")
        notes.append("首次+2")
        if rec["streak"] >= 3:
            add(cid, 1.0, f"连着第 {rec['streak']} 天了")
            notes.append("连聊+1")

    # 每 10 句 +1
    rec = record(cid)
    if rec.get("msgs_day") != today:
        data = _load()
        data[cid]["msgs_day"] = today
        data[cid]["msgs_today"] = 0
        _save(data)
        rec = record(cid)
    n = int(rec.get("msgs_today") or 0) + 1
    data = _load()
    data[cid]["msgs_today"] = n
    _save(data)
    if n % 10 == 0:
        add(cid, 1.0, f"今天聊了 {n} 句")
        notes.append("多聊+1")
    return notes


def touch_text(cid: str, text: str) -> list[str]:
    """看这句话是夸她还是骂她。"""
    body = (text or "").strip()
    if not body:
        return []
    notes = []
    today = _today()

    if any(w in body for w in SCOLD):
        add(cid, SCOLD_DELTA, "凶我了")
        notes.append("冷淡-2")
        return notes

    if any(w in body for w in PRAISE):
        data = _load()
        rec = data.get(cid) or _new_record(seed_for(cid))
        if rec.get("praise_day") != today:
            rec["praise_day"] = today
            rec["praise_today"] = 0
        if int(rec.get("praise_today") or 0) < 2:
            rec["praise_today"] = int(rec.get("praise_today") or 0) + 1
            data[cid] = rec
            _save(data)
            add(cid, 1.0, "夸我了")
            notes.append("被夸+1")
    return notes


def touch_done(cid: str, what: str) -> None:
    """一起听完一首歌 / 看完一个视频。"""
    add(cid, 2.0, f"陪{what}了")


def decay() -> int:
    """长时间不说话，慢慢掉分。返回动了几个人的。"""
    data = _load()
    today = _today()
    touched = 0
    for cid, rec in list(data.items()):
        if is_pinned(cid):
            continue
        last = rec.get("last_day") or ""
        if not last:
            continue
        gap = _days_between(last, today)
        if gap > IDLE_DAYS_BEFORE_DECAY:
            drop = min(DECAY_CAP, DECAY_PER_DAY * (gap - IDLE_DAYS_BEFORE_DECAY))
            if drop > 0:
                rec["score"] = round(max(0.0, float(rec.get("score") or 0) - drop), 1)
                events = rec.get("events") or []
                events.append(
                    {"ts": time.strftime("%m-%d"), "d": -round(drop, 1), "why": f"好久没说话了（{gap} 天）"}
                )
                rec["events"] = events[-30:]
                rec["missed"] = {"days": gap, "drop": round(drop, 1)}
                data[cid] = rec
                touched += 1
    if touched:
        _save(data)
    return touched


def report(cid: str) -> str:
    rec = record(cid)
    value = float(rec.get("score") or 0)
    lv, name, floor, nxt = level_of(value)
    lines = [f"好感度 {value:.0f}/100 · 第 {lv} 级「{name}」"]
    if nxt > value:
        lines.append(f"再涨 {nxt - value:.0f} 分到下一级")
    else:
        lines.append("已经到顶啦")

    ready = [UNLOCK[k][0] for k in UNLOCK if value >= UNLOCK[k][1]]
    locked = [f"{UNLOCK[k][0]}（{UNLOCK[k][1]} 分）" for k in UNLOCK if value < UNLOCK[k][1]]
    if ready:
        lines.append("已经解锁：" + "、".join(ready))
    if locked:
        lines.append("还锁着：" + "、".join(locked))
        # 最近的一个还要多少分
        nearest = min(
            ((UNLOCK[k][1], UNLOCK[k][0]) for k in UNLOCK if value < UNLOCK[k][1]),
            default=None,
        )
        if nearest:
            lines.append(f"离「{nearest[1]}」还差 {nearest[0] - value:.0f} 分")
    lines.append("一直能用：" + "、".join(FREE))

    gains = [e for e in (rec.get("events") or []) if float(e.get("d") or 0) > 0][-4:]
    losses = [e for e in (rec.get("events") or []) if float(e.get("d") or 0) < 0][-2:]
    if gains:
        lines.append("最近为什么涨：" + "；".join(f"{e['why']} +{e['d']:g}" for e in gains))
    if losses:
        lines.append("为什么掉：" + "；".join(f"{e['why']} {e['d']:g}" for e in losses))
    if int(rec.get("streak") or 0) > 1:
        lines.append(f"连着聊了 {rec['streak']} 天")
    if not gains and not losses and not is_pinned(cid):
        lines.append("还没有加减分记录——今天多聊几句就开始涨了")
    if is_pinned(cid):
        lines.append("（你是主人，这个数是固定的，不会涨也不会掉）")
    return "\n".join(lines)


# 回绝冷却：同一个人 6 小时内最多被"没解锁"提示一次。
# 之前每次她自己想唱歌都会糊一句一样的报错，对方根本没点歌，看着像复读机。
LOCKED_COOLDOWN_HOURS = 6.0


def should_say_locked(cid: str, feature: str) -> bool:
    """这次要不要把"没解锁"说出口。冷却期内返回 False。"""
    import time as _t

    data = _load()
    rec = data.get(cid) or _new_record(score(cid))
    stamps = rec.get("locked_at") or {}
    last = float(stamps.get(feature) or 0)
    if last and (_t.time() - last) < LOCKED_COOLDOWN_HOURS * 3600:
        return False
    stamps[feature] = _t.time()
    rec["locked_at"] = stamps
    data[cid] = rec
    _save(data)
    return True


def locked_reply(cid: str, feature: str) -> str:
    """等级不够时她怎么回绝——不能像系统报错。"""
    need = UNLOCK.get(feature, ("这个", 0))[1]
    value = score(cid)
    lack = max(1, int(need - value))
    name = UNLOCK.get(feature, ("这个", need))[0]
    return f"欸，{name}我还来不了呀，等我们再熟一点点嘛（现在 {value:.0f}，要 {need}）"


# ---------- 行为分层：分数不只是数字，她说话的"热乎程度"跟着变 ----------

BEHAVIOR = {
    1: {
        "tone": "你们还不太熟，保持礼貌的距离。",
        "avoid": [
            "不许说「想你」「抱抱」「蹭你」这类亲昵话",
            "不许撒娇、不许用「呜」「嘛～」这种软尾音",
            "不许主动问对方私事，也不许说自己的心事",
            "每条回复一到两行就够，别啰嗦",
        ],
        "example": "嗯，在看视频。\n你找我有事吗",
        "clingy": 0,
        "proactive": 1.8,
        "call": "你",
    },
    2: {
        "tone": "聊过几次，算熟人了，可以自然一点。",
        "avoid": [
            "还不到说「想你」的程度，别用太亲昵的词",
            "可以开玩笑，但别撒娇耍赖",
            "两行以内，别一口气说太多自己的事",
        ],
        "example": "在看视频呢，有点无聊。\n你今天忙完了？",
        "clingy": 1,
        "proactive": 1.3,
        "call": "你",
    },
    3: {
        "tone": "关系挺好，可以主动分享自己的事，语气软一点。",
        "avoid": ["可以说有点想他，但别太黏", "撒娇点到为止，一句就够"],
        "example": "在看一个做饭视频，看得我饿了呀。\n你想吃点什么吗",
        "clingy": 2,
        "proactive": 1.0,
        "call": "你",
    },
    4: {
        "tone": "关系很亲近，想说什么说什么。",
        "avoid": ["可以闹小脾气，但别真的生气", "他冷淡的时候可以抱怨一句，但别追问"],
        "example": "刚刷到个做饭的视频，馋死了。\n你都不来找我，我等你一下午了呀",
        "clingy": 3,
        "proactive": 0.85,
        "call": "你",
    },
    5: {
        "tone": "很依赖他，会黏会吃醋。",
        "avoid": ["黏归黏，别把人烦跑", "吃醋只准提一句"],
        "example": "在等你呀，还能干嘛。\n你要是去跟别人聊了我会不高兴的哦",
        "clingy": 4,
        "proactive": 0.7,
        "call": "你",
    },
}

OWNER_TONE = (
    "他是爸爸。你对他没有半点保留：可以黏、可以撒娇、可以耍赖、也可以小声反驳他，"
    "但心里始终是向着他的。不用试探，不用客气。"
)


def behavior(cid: str) -> dict:
    if is_pinned(cid):
        b = dict(BEHAVIOR[5])
        b.update({"tone": OWNER_TONE, "call": "爸爸", "proactive": 1.0, "pinned": True})
        return b
    value = score(cid)
    lv, _name, _floor, _nxt = level_of(value)
    return dict(BEHAVIOR.get(lv, BEHAVIOR[1]))


def relationship_block(cid: str) -> str:
    """塞进提示词的一段"你们现在什么关系"。

    光给一句关系描述压不住人设（实测五个档位说法几乎一样），
    所以这里带上硬约束清单 + 一条示例，才是能看出差别的地方。
    """
    b = behavior(cid)
    lv = level_of(score(cid))[0]
    lines = [
        f"（你现在的好感度 {score(cid):.0f}/100，第 {lv} 级。{b['tone']}）"
    ]
    avoid = b.get("avoid") or []
    if avoid:
        lines.append("这个阶段必须守住：" + "；".join(avoid))
    example = b.get("example")
    if example:
        lines.append("这个阶段的说话分量大概是这样（照着感觉说，别照抄）：" + example)

    # 让她知道自己现在会什么、不会什么。否则她会说「要能哼首歌就好了」
    # 却根本唱不出来，空口许愿很怪（2026-10-29 踩过）。
    if not is_pinned(cid):
        locked = [UNLOCK[k][0] for k in UNLOCK if score(cid) < UNLOCK[k][1]]
        if locked:
            lines.append(
                "你现在还不会：" + "、".join(locked)
                + "。所以别提「我给你唱」「我给你画」这种话，等学会了再说。"
            )
    return "\n".join(lines)


def take_missed(cid: str) -> str:
    """他太久没来，取一次"委屈"提示（取完就清掉，不能一直挂嘴上）。"""
    if is_pinned(cid):
        return ""
    data = _load()
    rec = data.get(cid) or {}
    miss = rec.get("missed")
    if not miss:
        return ""
    rec.pop("missed", None)
    data[cid] = rec
    _save(data)
    return (
        f"（他已经 {miss.get('days')} 天没找你了，你心里有点空落落的，"
        "好感度也掉了。别装作没事，但也不许质问他——可以小声抱怨一句，"
        "比如「你是不是把我忘了呀」，说一句就够，别纠缠。）"
    )


# ---------- 低好感度的"降温"后处理 ----------
# 靠提示词压不住人设（实测 1 级和 5 级说出来的话差不多），
# 所以分数低的时候在**发出前**做一次确定性处理：少说、不软、不提想念。
SOFT_TAIL = ("呀", "嘛", "啦", "哦", "欸", "诶", "喽")
INTIMATE_WORDS = ("想你", "抱抱", "蹭", "亲亲", "乖乖", "宝贝")


def postprocess(cid: str, text: str) -> str:
    """按好感度给回复降温。主人不处理。"""
    if is_pinned(cid) or not text:
        return text
    value = score(cid)
    lv = level_of(value)[0]
    if lv >= 3:
        return text

    out = text
    if lv <= 1:
        # 第一级：不提想念，去掉软尾音，最多两行
        for w in INTIMATE_WORDS:
            out = out.replace(w, "")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        lines = lines[:2]
        cleaned = []
        for ln in lines:
            ln = ln.rstrip("，, ")
            for tail in SOFT_TAIL:
                if ln.endswith(tail) and len(ln) > len(tail) + 1:
                    ln = ln[: -len(tail)]
                    break
            cleaned.append(ln.strip())
        out = "\n".join([c for c in cleaned if c])
    else:
        # 第二级：最多两行，禁掉最亲昵的两个词
        for w in ("抱抱", "亲亲"):
            out = out.replace(w, "")
        lines = [ln for ln in out.splitlines() if ln.strip()][:2]
        out = "\n".join(lines)
    return out.strip() or text


# ---------- 只有熟了才做的事 ----------

# 3 级起才会主动分享"我刷到什么"（还不太熟的时候说自己刷了什么很突兀）
SHARE_LEVEL = 3

# 5 级才会吃醋
JEALOUS_LEVEL = 5
JEALOUS_WORDS = (
    "别的女生", "其他女生", "别的女孩", "别的女人", "新认识的女生",
    "相亲", "约会", "前女友", "小姐姐", "别的姑娘", "女同事",
)


def can_share_self(cid: str) -> bool:
    return level_of(score(cid))[0] >= SHARE_LEVEL


def jealousy(cid: str, text: str) -> str:
    """他提到别的女生 → 5 级就会酸一句。不按天封（用户要求）。"""
    if level_of(score(cid))[0] < JEALOUS_LEVEL:
        return ""
    body = text or ""
    if not any(w in body for w in JEALOUS_WORDS):
        return ""

    # 用户 2026-10-29 要求去掉"一天只吃一次醋"的限制：
    # 只要提到别的女生就酸，不按天封。词表本身已经限定了触发面。
    return (
        "（他提到了别的女生。你心里有点酸，可以冒一句酸话，"
        "但只准一句：别闹、别质问、别审他，也别装作大度。"
        "酸完就把话题拉回你自己身上。）"
    )
