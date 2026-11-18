"""对方提出的"作息要求"：抽成结构化规则，并且**由代码强制执行**。

为什么必须有这个模块：
截图里朋友说「以后九点多之后再给我发消息」，她回「好，这下我记全了」，
然后照旧早上发。根因是——**约束只存在于对话里，没有任何一处代码读它**。
模型"答应"是最廉价的行为，它不会真的记住，也不会在主动消息时检查。

所以分三步：
1. `extract()` 把自然语言抽成 JSON（安静时段 / 不主动 / 最小间隔）
2. `allows_proactive()` 在**发主动消息之前**硬拦（这是真正生效的地方）
3. `describe()` 把规则回灌提示词，她才能准确复述；含糊的规则要反问确认

规则含糊或自相矛盾时**不要硬记**，先反问确认（`pending`）。
"""

from __future__ import annotations

import json
import re
import time

from whale import config

PATH = config.DATA_DIR / "rules.json"

# 只有这些话里才值得花一次模型调用去抽规则（省钱，也避免误判）
HINT_WORDS = (
    "以后", "之后", "别", "不要", "不许", "不用", "尽量", "安静", "吵",
    "点前", "点后", "点之前", "点之后", "早上", "晚上", "中午", "周末", "周六", "周日",
    "主动", "发消息", "找我", "打扰", "睡觉", "上班", "上课",
)

CONFIRM_WORDS = ("对", "是的", "是", "嗯", "没错", "可以", "就这样", "准", "正确", "ok", "好")

# 他主动提了某个禁区话题之后，这个话题"开放"多久——
# 在这段时间里她可以顺着聊，否则他一说"还行"她就接不下去，对话会被切断。
TOPIC_OPEN_MIN = 45
DENY_WORDS = ("不对", "不是", "错了", "改一下", "重新")

_WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


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


def _rec(data: dict, cid: str) -> dict:
    r = data.get(cid)
    if not r:
        r = {"quiet": [], "no_proactive": False, "min_gap_min": 0,
             "note": "", "pending": None, "hits": 0, "violations": 0, "updated": 0}
        data[cid] = r
    return r


# ---------- 解析 ----------

EXTRACT_PROMPT = """把对方这段话里的"对我的要求"抽成 JSON。只输出 JSON，不要解释。

字段：
- no_sticker: true 表示"别发表情包/别发图"（说的是表情包、贴图、沙雕图）
- quiet: 数组，安静时段（这段时间别主动找他）。每项 {"start":"HH:MM","end":"HH:MM","days":[...] 或 "all"}
  days 用 0=周一 … 6=周日；只对某几天生效才写数组，否则写 "all"
- no_proactive: true 表示"完全不要主动找我"
- min_gap_min: 两条主动消息之间至少隔多少分钟（没说就 0）
- topics: 数组，他**不喜欢你提的话题**。每项 2~6 字，而且**同一件事要列全常见说法**
  （最多 6 个），因为拦截是逐字匹配的，只给一种说法会漏。
  例如「别老问我吃了没」→ ["吃了没","吃饭","吃了吗","吃什么","吃了什么","吃啥"]
  例如「以后别跟我聊游戏了」→ ["游戏","打游戏","游戏局"]
- remind: 数组，到点给他发消息的约定。
  每项 {"at":"HH:MM","when":"today|tomorrow","repeat":"once|daily|weekdays","note":"要说什么，没有就空"}
  repeat: 说"每天/每晚"→ daily；说"工作日/周一到周五"→ weekdays；没说 → once
  注意：这是"到点发一条"，不是安静时段，别放进 quiet。
  **上午/晚上的歧义必须标 ambiguous**：他说"11点20分"但没说上午还是晚上，写 true。
- summary: 用一句中文复述你的理解（不超过 40 字）
- ambiguous: 如果你不确定或话里自相矛盾，写 true

例子：
输入：以后九点多之后再给我发消息
输出：{"quiet":[{"start":"00:00","end":"09:20","days":"all"}],"no_proactive":false,"min_gap_min":0,"summary":"九点二十以前别主动找他","ambiguous":false}

输入：别老问我吃了没，烦
输出：{"quiet":[],"no_proactive":false,"min_gap_min":0,"topics":["吃了没","吃饭","吃了吗","吃什么","吃了什么","吃啥"],"summary":"别追着问他吃没吃饭","ambiguous":false}

输入：以后别发表情包了
输出：{"quiet":[],"no_proactive":false,"min_gap_min":0,"topics":[],"no_sticker":true,"remind":[],"summary":"以后不发表情包","ambiguous":false}

输入：今晚11点20分给我发信息
输出：{"quiet":[],"no_proactive":false,"min_gap_min":0,"topics":[],"remind":[{"at":"23:20","when":"today","note":""}],"summary":"今晚十一点二十给你发一条","ambiguous":false}

输入：平时九点前不主动，周六六点前可以，六到十五点安静
输出：{"quiet":[{"start":"00:00","end":"09:00","days":[0,1,2,3,4]},{"start":"06:00","end":"15:00","days":"all"}],"no_proactive":false,"min_gap_min":0,"summary":"工作日九点前、每天六点到十五点别主动找他","ambiguous":true}

对方的话：
"""


def _valid_hm(s: str) -> str:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s or "").strip())
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 24 and 0 <= mi <= 59):
        return ""
    if h == 24:
        h, mi = 23, 59
    return f"{h:02d}:{mi:02d}"


def _clean(raw: dict) -> dict:
    """模型输出不可信，逐字段校验。"""
    quiet = []
    for item in (raw.get("quiet") or [])[:6]:
        if not isinstance(item, dict):
            continue
        a, b = _valid_hm(item.get("start")), _valid_hm(item.get("end"))
        if not a or not b or a == b:
            continue
        days = item.get("days")
        if days == "all" or days is None:
            days = "all"
        elif isinstance(days, list):
            days = sorted({int(d) for d in days if str(d).isdigit() and 0 <= int(d) <= 6})
            if len(days) == 7:
                days = "all"
        else:
            days = "all"
        quiet.append({"start": a, "end": b, "days": days})
    topics = []
    for t in (raw.get("topics") or [])[:10]:
        t = str(t or "").strip().strip("「」\"'“” ")
        if 2 <= len(t) <= 6 and t not in topics:
            topics.append(t)

    remind = []
    for item in (raw.get("remind") or [])[:3]:
        if not isinstance(item, dict):
            continue
        at = _valid_hm(item.get("at"))
        if not at:
            continue
        when = str(item.get("when") or "today").strip()
        if when not in ("today", "tomorrow"):
            when = "today"
        repeat = str(item.get("repeat") or "once").strip()
        if repeat not in ("once", "daily", "weekdays"):
            repeat = "once"
        remind.append({"at": at, "when": when, "repeat": repeat,
                       "note": str(item.get("note") or "")[:60]})

    return {
        # 关键：模型**没提这件事**时必须给 None，不能给 False。
        # False 的语义是"他说可以发"，会把之前设的"别发表情包"冲掉。
        # （GitHub issue #1 @mattabate 指出：_clean 永远返回 bool，
        #   save() 里 is not None 的判断就形同虚设）
        "no_sticker": (
            None if "no_sticker" not in raw else bool(raw.get("no_sticker"))
        ),
        "remind": remind,
        "topics": topics,
        "quiet": quiet,
        "no_proactive": bool(raw.get("no_proactive")),
        "min_gap_min": max(0, min(240, int(raw.get("min_gap_min") or 0))),
        "summary": str(raw.get("summary") or "")[:60],
        "ambiguous": bool(raw.get("ambiguous")),
    }


# 时间说法单独用正则认：光靠词表会漏「今晚11点20分给我发信息」这种
_TIME_RE = re.compile(
    r"(\d{1,2}\s*[点:：]\s*\d{0,2})"          # 11点20 / 11:20 / 7点
    # 中文数字：排除「一点点」这种（点后面不能再跟点）
    r"|([一二三四五六七八九十]{1,3}\s*点半?(?!点))"
)
_EXTRA_HINTS = ("提醒", "叫我", "喊我", "发消息", "发信息", "别找我", "别主动", "叫我起床")


_STICKER_WORDS = ("表情包", "表情", "贴图", "沙雕图", "图包")
_FORBID_WORDS = ("别", "不要", "不许", "不准", "不能", "不可以", "禁止", "少发", "别再")
_ALLOW_WORDS = ("可以", "能发", "允许", "准许", "发吧", "放开", "不用禁", "想发就发")


def sticker_permission(text: str) -> str:
    """他说的是"不准发表情包"还是"可以发表情包"。

    返回 "forbid" / "allow" / ""（没提）。
    为什么要单独判：两个要求自相矛盾时，**必须按最后一次说的来**，
    而"可以发表情包了"这种话在词表和正则里都触发不到抽取（踩过）。
    """
    body = text or ""
    if not any(w in body for w in _STICKER_WORDS):
        return ""
    # "不用禁了""别禁了"这类反向说法先排除
    if any(w in body for w in ("不用禁", "别禁", "不用管了", "算了")):
        return "allow"
    if any(w in body for w in _FORBID_WORDS):
        return "forbid"
    if any(w in body for w in _ALLOW_WORDS):
        return "allow"
    return ""


def wants_extract(text: str) -> bool:
    body = (text or "").strip()
    if len(body) < 4 or len(body) > 200:
        return False
    if any(w in body for w in HINT_WORDS):
        return True
    if any(w in body for w in _EXTRA_HINTS):
        return True
    if sticker_permission(body):
        return True
    # 光有时间还不够（「我一点累」也算时间），得同时是"在提要求"
    if _TIME_RE.search(body) and any(
        w in body for w in ("给", "发", "叫", "喊", "提醒", "别", "找", "等", "到点")
    ):
        return True
    return False


async def extract(text: str) -> dict:
    """调一次模型抽规则。模型挂了就返回空。"""
    from whale.agent import llm

    try:
        out = await llm.chat(
            [{"role": "user", "content": EXTRACT_PROMPT + text}],
            temperature=0.1,
            max_tokens=300,
        )
    except Exception:
        return {}
    raw = (out or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        return _clean(json.loads(m.group(0)))
    except Exception:
        return {}


# ---------- 存 / 查 ----------

def save(cid: str, parsed: dict, note: str = "") -> None:
    data = _load()
    rec = _rec(data, cid)
    if parsed.get("quiet"):
        rec["quiet"] = parsed["quiet"]
    if parsed.get("no_proactive"):
        rec["no_proactive"] = True
    if parsed.get("min_gap_min"):
        rec["min_gap_min"] = parsed["min_gap_min"]
    if parsed.get("topics"):
        # 新说的覆盖旧的（他说"别聊 X"就是整个换掉）
        rec["topics"] = parsed["topics"]
    # 三种情况要分清：
    #   字段不存在 / 是 None → 他这次没提，**别动**原来的设置
    #   True                → 禁
    #   False               → 解禁（他说"可以发了"）
    # 只写 if parsed.get("no_sticker") 会变成"只能禁、不能解禁"（踩过）；
    # 而如果 _clean 永远给 False，又会变成"提别的事就把禁言冲掉"（issue #1）。
    if parsed.get("no_sticker") is not None:
        rec["no_sticker"] = bool(parsed["no_sticker"])
    rec["note"] = note or parsed.get("summary") or rec.get("note") or ""
    rec["pending"] = None
    rec["updated"] = time.time()
    _save(data)


def set_pending(cid: str, parsed: dict) -> None:
    data = _load()
    rec = _rec(data, cid)
    rec["pending"] = parsed
    rec["pending_at"] = time.time()
    _save(data)


def pending_of(cid: str) -> dict:
    rec = _rec(_load(), cid)
    p = rec.get("pending") or {}
    if not p:
        return {}
    if time.time() - float(rec.get("pending_at") or 0) > 6 * 3600:
        return {}
    return p


def discard_pending(cid: str) -> None:
    data = _load()
    rec = _rec(data, cid)
    rec["pending"] = None
    _save(data)


def clear(cid: str) -> None:
    data = _load()
    if cid in data:
        data.pop(cid, None)
        _save(data)


def has_rules(cid: str) -> bool:
    rec = _rec(_load(), cid)
    return bool(rec.get("quiet") or rec.get("no_proactive")
                or rec.get("min_gap_min") or rec.get("topics"))


def no_sticker(cid: str) -> bool:
    """他明确说过"别发表情包"吗。"""
    return bool(_rec(_load(), cid).get("no_sticker"))


def clear_no_sticker(cid: str) -> None:
    data = _load()
    rec = _rec(data, cid)
    rec["no_sticker"] = False
    _save(data)


def topics_of(cid: str) -> list[str]:
    return list(_rec(_load(), cid).get("topics") or [])


def _variants(topic: str) -> set[str]:
    """话题的"同族说法"。

    「吃饭」和「吃了没」其实是一件事，但字面对不上：
    他说「我今天吃了火锅」，按字面匹配不到「吃饭」，就会误判成"她主动提"。
    所以取前两个字当同族词（吃了 / 吃饭），够用又不会宽到离谱。
    """
    out = {topic}
    if len(topic) >= 3:
        out.add(topic[:2])
    return {w for w in out if len(w) >= 2}


def check_topic(cid: str, her_text: str, his_text: str = "",
                ts: float | None = None) -> str:
    """她这句话有没有踩到他不想聊的话题。

    规则分三层（用户 2026-10-29 明确要求）：
    1. **他这条消息就在聊这个** → 放行（顺着他说）
    2. **他刚才提过、还在开放窗口内**（TOPIC_OPEN_MIN 分钟）→ 也放行，
       否则他说完"吃了火锅"、下一条说"还行"，她就接不下去，对话被硬切断
    3. 否则她主动提 → 拦

    窗口只由"他提到"来续期，她自己反复提不会一直续——
    不然她能靠自言自语把这个壁垒拖到永久。
    """
    topics = topics_of(cid)
    if not topics:
        return ""
    ts = ts or time.time()
    body = her_text or ""
    his = his_text or ""

    family = set()
    for t in topics:
        family |= _variants(t)

    opened = _open_topic(cid, his, family, ts)
    if opened:
        return ""      # 他自己聊起来了，话题开着

    for t in topics:
        if t in body and not _topic_is_open(cid, t, family, ts):
            return t
    return ""


def _open_topic(cid: str, his: str, family: set[str], ts: float) -> bool:
    """他这条消息提到某个禁区话题 → 记下开放时间。"""
    if not his:
        return False
    hit = any(w in his for w in family)
    if not hit:
        return False
    data = _load()
    rec = _rec(data, cid)
    opened = rec.get("topic_open") or {}
    for t in rec.get("topics") or []:
        if any(w in his for w in _variants(t)):
            opened[t] = ts
    rec["topic_open"] = opened
    _save(data)
    return True


def _topic_is_open(cid: str, topic: str, family: set[str], ts: float) -> bool:
    rec = _rec(_load(), cid)
    opened = rec.get("topic_open") or {}
    mine = [opened.get(t) for t in rec.get("topics") or [] if t == topic]
    last = max([float(v) for v in mine if v] or [0])
    return bool(last) and (ts - last) < TOPIC_OPEN_MIN * 60


def note_topic_hit(cid: str, topic: str) -> None:
    data = _load()
    rec = _rec(data, cid)
    rec["topic_hits"] = int(rec.get("topic_hits") or 0) + 1
    rec["last_topic_hit"] = {"ts": time.strftime("%m-%d %H:%M"), "topic": topic}
    _save(data)


def _in_window(now_min: int, start: str, end: str) -> bool:
    a = int(start[:2]) * 60 + int(start[3:])
    b = int(end[:2]) * 60 + int(end[3:])
    if a <= b:
        return a <= now_min < b
    return now_min >= a or now_min < b   # 跨午夜


def allows_proactive(cid: str, ts: float | None = None) -> tuple[bool, str]:
    """现在能不能主动给他发消息。这是真正生效的闸门。"""
    ts = ts or time.time()
    rec = _rec(_load(), cid)
    if rec.get("no_proactive"):
        return False, "他说过别主动找他"
    lt = time.localtime(ts)
    now_min = lt.tm_hour * 60 + lt.tm_min
    for w in rec.get("quiet") or []:
        days = w.get("days", "all")
        if days != "all" and lt.tm_wday not in days:
            continue
        if _in_window(now_min, w["start"], w["end"]):
            return False, f"他的安静时段 {w['start']}-{w['end']}"

    gap = int(rec.get("min_gap_min") or 0)
    if gap:
        last = float(rec.get("last_proactive") or 0)
        if last and (ts - last) < gap * 60:
            return False, f"他说两条主动消息至少隔 {gap} 分钟"
    return True, ""


def note_proactive(cid: str) -> None:
    data = _load()
    rec = _rec(data, cid)
    rec["last_proactive"] = time.time()
    _save(data)


def note_violation(cid: str, why: str) -> None:
    data = _load()
    rec = _rec(data, cid)
    rec["violations"] = int(rec.get("violations") or 0) + 1
    rec["last_violation"] = {"ts": time.strftime("%m-%d %H:%M"), "why": why}
    _save(data)


def describe(cid: str) -> str:
    """给人看 / 给提示词用的一句话。"""
    rec = _rec(_load(), cid)
    parts = []
    for w in rec.get("quiet") or []:
        days = w.get("days", "all")
        if days == "all":
            when = "每天"
        else:
            when = "、".join(_WEEK[d] for d in days)
        parts.append(f"{when} {w['start']}-{w['end']} 别主动")
    if rec.get("no_proactive"):
        parts.append("完全别主动找他")
    if rec.get("min_gap_min"):
        parts.append(f"主动消息间隔至少 {rec['min_gap_min']} 分钟")
    if rec.get("topics"):
        parts.append("不许提：" + "、".join(rec["topics"]))
    if rec.get("no_sticker"):
        parts.append("别发表情包")
    if not parts:
        return ""
    return "；".join(parts)


def report(cid: str) -> str:
    rec = _rec(_load(), cid)
    body = describe(cid)
    lines = []
    if not body:
        lines.append("我现在没记着什么事。")
        lines.append("直接说就行，比如「以后九点前别主动找我」或者「11点半叫我」。")
        return "\n".join(lines)
    lines.append("我记着这些：")
    lines.append("· " + body)
    if rec.get("note"):
        lines.append(f"（他当时的原话：{rec['note']}）")
    if rec.get("violations"):
        lv = rec.get("last_violation") or {}
        lines.append(f"我违反过 {rec['violations']} 次，最近一次 {lv.get('ts','')}（{lv.get('why','')}）")
    if rec.get("topic_hits"):
        lh = rec.get("last_topic_hit") or {}
        lines.append(f"不小心提过 {rec['topic_hits']} 次，最近一次 {lh.get('ts','')}（{lh.get('topic','')}）")
    lines.append("要清掉：/事件 清空")
    return "\n".join(lines)


# ---------- 上午/晚上 的歧义（不信任模型的说法，代码自己判） ----------
PERIOD_WORDS = ("上午", "早上", "早晨", "凌晨", "中午", "下午", "傍晚", "晚上", "今晚", "夜里", "半夜", "明早")
_NEAR_ENOUGH = 6 * 3600   # 6 小时内能到的时间点，不用问上午还是晚上


def _hm_to_min(at: str) -> int:
    return int(at[:2]) * 60 + int(at[3:])


def time_question(text: str, at: str, now: float | None = None) -> str:
    """要不要反问"上午还是晚上"。返回问题，不需要问就返回空。

    判据（按优先级）：
    1. 原话里已经说了 上午/下午/晚上/今晚… → 不问
    2. 小时数 > 12（模型已给 23:20 这种） → 不问
    3. 最近的时间点在 6 小时内 → 不问（他 23:25 说 11 点半，就是 5 分钟后）
    4. **只有"最近的点是靠 +12 小时翻出来的"才问** ——
       比如 11:35 说「11点半叫我」，今天 11:30 已过，最近的是今晚 23:30（翻出来的），
       这时他很可能其实想说"明天 11:30"，必须问。
    """
    import time as _t
    from whale.behavior import remind

    now = now or _t.time()
    body = text or ""
    if any(w in body for w in PERIOD_WORDS):
        return ""
    try:
        hh = int(at[:2])
    except Exception:
        return ""
    if hh > 12:
        return ""

    near = remind.nearest_at(at, now)
    if near - now <= _NEAR_ENOUGH:
        return ""

    # near 是不是"翻出来的"：同一钟点（今天/明天）里有没有更近的？
    lt = _t.localtime(now)
    same = []
    for day_off in (0, 1):
        ts = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + day_off,
                        hh, int(at[3:]), 0, 0, 0, -1))
        if ts > now + 30:
            same.append(ts)
    # near 落在"原钟点"上就按原意来，只有落在 +12 小时那个点上才需要问
    # （之前写成 min(same) < near - 60，导致凌晨说「11点半」也会被反问）
    if any(abs(near - ts) < 60 for ts in same):
        return ""

    am = f"{hh}:{at[3:]}"
    pm_h = hh + 12
    return f"你是说上午 {am} 还是晚上 {pm_h}:{at[3:]}"


def past_question(text: str, at: str, now: float | None = None) -> str:
    """刚说的时间点"今天已经过去了"→ 反问是不是指明天。

    为什么需要（踩过）：23:44 说「晚上11点半」，"晚上"已经说清了所以不反问，
    但今天 23:30 过去 14 分钟了，代码顺延到**明天 23:30**——排到 23 小时后还不问，
    用户第二天晚上才被喊。
    判据：要排的时间离现在超过 6 小时，且"今天/明天"里更近的那个已经过去不到 12 小时。
    """
    import time as _t
    from whale.behavior import remind

    now = now or _t.time()
    try:
        hh, mm = int(at[:2]), int(at[3:])
    except Exception:
        return ""

    near = remind.nearest_at(at, now)
    if near - now <= _NEAR_ENOUGH:
        return ""      # 6 小时内，正常

    lt = _t.localtime(now)
    past = []
    for day_off in (0, -1):
        ts = _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + day_off,
                        hh, mm, 0, 0, 0, -1))
        if now - 12 * 3600 < ts < now:
            past.append(ts)
    if not past:
        return ""
    last = max(past)
    when = "今天" if _t.localtime(last).tm_mday == lt.tm_mday else "昨天"
    return (f"{when} {at} 已经过去 {int((now - last) // 60)} 分钟了，"
            f"你是说明天 {at} 吗")


def apply_period(parsed: dict, answer: str) -> dict:
    """用户回答"上午/晚上"之后，把那一条的时间改过来。"""
    body = answer or ""
    shift = 0
    if any(w in body for w in ("下午", "晚上", "傍晚", "今晚", "夜里", "半夜")):
        shift = 12
    elif any(w in body for w in ("上午", "早上", "早晨", "凌晨", "明早")):
        shift = -12
    if not shift:
        return parsed
    for item in parsed.get("remind") or []:
        at = str(item.get("at") or "")
        try:
            hh, mm = int(at[:2]), int(at[3:])
        except Exception:
            continue
        if shift > 0 and hh < 12:
            hh += 12
        elif shift < 0 and hh >= 12:
            hh -= 12
        item["at"] = f"{hh:02d}:{mm:02d}"
    return parsed

def prompt_block(cid: str) -> str:
    from whale.behavior import remind

    body = describe(cid)
    soon = remind.describe(cid)
    if soon:
        body = (body + "；" if body else "") + "约好到点要发：" + soon
    if not body:
        return ""
    return (
        f"（他对你提过要求：{body}。这是硬规则：安静时段系统会拦住你的主动消息；"
        "**他让你几点给他发消息，你一定能做到**——系统会到点叫你，别推脱说"
        "「保证不了」「不知道那时候醒没醒」，那种话他会当成你答应了又不做。"
        "**别发表情包**（他说过不许发；系统会拦住，你别写 [[贴图:…]] 标记）。"
        "话题禁区**别自己开话头**，但他自己先提起来的时候你可以顺着聊，"
        f"聊起来之后 {TOPIC_OPEN_MIN} 分钟内都可以接着聊，过了这段时间就别再翻出来了。"
        "你复述的时候要按这条说，别自己改。）"
    )
