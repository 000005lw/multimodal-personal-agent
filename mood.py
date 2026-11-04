"""情绪：会生气、会闹别扭、也会被哄好。

设计取舍（重要）：
1. **不是真的情绪**，是状态机 + 提示词。别自欺欺人。
2. **沉默有硬上限**：最多忽略 3 条、最多 40 分钟，之后必须回。
   否则你会以为机器人挂了（这个错很贵：掉线是要扫码的）。
3. **急事必须回**：带「急」「救命」「出事」或者连发两条「在吗」，
   无论多生气都得回话。人身安全比闹别扭重要。
4. **不会永久记仇**：24 小时内自己消气，情绪慢慢回到 0。
5. 只有好感度 ≥ 40（亲近）才会闹别扭——关系不到位的人，她只是冷淡，不是生气。
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import config

PATH = config.DATA_DIR / "mood.json"

MOOD_MIN, MOOD_MAX = -3.0, 3.0

# 生气阈值与恢复
SULK_THRESHOLD = -2.0
SULK_LEVEL = 40.0          # 好感度到这个数才会闹别扭
MAX_IGNORE = 3             # 最多忽略几条
IGNORE_WINDOW = 40 * 60    # 一次闹别扭最长多久（秒）
FORGET_AFTER = 24 * 3600   # 多久之后自己消气

SCOLD_WORDS = ("滚", "烦死", "废物", "讨厌你", "没用", "闭嘴", "傻逼", "去死", "妈的", "滚蛋", "垃圾")
APOLOGY_WORDS = ("对不起", "抱歉", "我错了", "别生气", "不生气了", "哄哄", "不该那样", "我不好", "原谅")
PRAISE_WORDS = ("可爱", "乖", "好棒", "厉害", "喜欢你", "爱你", "心疼", "辛苦", "想你", "好听")

# 急事：再生气也要回
URGENT_WORDS = ("急", "救命", "出事", "快回", "出事了", "急诊", "医院", "报警")


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
        r = {"mood": 0.0, "why": [], "sulk_at": 0.0, "ignored": 0, "updated": time.time()}
        data[cid] = r
    return r


def _log(rec: dict, delta: float, why: str) -> None:
    events = rec.get("why") or []
    events.append({"ts": time.strftime("%m-%d %H:%M"), "d": round(delta, 1), "why": why})
    rec["why"] = events[-15:]


def mood_of(cid: str) -> float:
    return float(_rec(_load(), cid).get("mood") or 0.0)


def _affection(cid: str) -> float:
    try:
        import affection
        return affection.score(cid)
    except Exception:
        return 0.0


def touch(cid: str, text: str) -> None:
    """从这句话里读情绪：被凶、被哄、被夸。"""
    body = (text or "").strip()
    if not body:
        return
    data = _load()
    rec = _rec(data, cid)
    delta = 0.0
    why = ""

    if any(w in body for w in URGENT_WORDS):
        # 急事优先，情绪先放一边
        return

    if any(w in body for w in APOLOGY_WORDS):
        delta, why = 1.5, "哄我了"
    elif any(w in body for w in SCOLD_WORDS):
        delta, why = -2.0, "凶我了"
    elif any(w in body for w in PRAISE_WORDS):
        delta, why = 1.0, "夸我了"

    if not delta:
        return
    rec["mood"] = round(max(MOOD_MIN, min(MOOD_MAX, float(rec.get("mood") or 0) + delta)), 1)
    _log(rec, delta, why)
    rec["updated"] = time.time()
    # 哄好了就退出闹别扭
    if rec["mood"] > SULK_THRESHOLD:
        rec["sulk_at"] = 0.0
        rec["ignored"] = 0
    _save(data)


def pleasant(cid: str, why: str = "陪我说话了") -> None:
    """加分事件（一起听歌、看视频）。"""
    data = _load()
    rec = _rec(data, cid)
    rec["mood"] = round(min(MOOD_MAX, float(rec.get("mood") or 0) + 1.0), 1)
    _log(rec, 1.0, why)
    rec["updated"] = time.time()
    _save(data)


def is_sulking(cid: str) -> bool:
    """在闹别扭吗。

    判据是"情绪够低 + 关系够近"，不是"曾经开始过"——
    之前写成必须先有 sulk_at 才能算闹，而 sulk_at 又要等第一次忽略才写，
    结果互相等，永远闹不起来（踩过）。
    """
    rec = _rec(_load(), cid)
    if float(rec.get("mood") or 0) > SULK_THRESHOLD:
        return False
    if _affection(cid) < SULK_LEVEL:
        return False
    started = float(rec.get("sulk_at") or 0)
    if started and time.time() - started > FORGET_AFTER:
        return False
    return True


def _mark_sulk_start(cid: str) -> dict:
    data = _load()
    rec = _rec(data, cid)
    if not rec.get("sulk_at"):
        rec["sulk_at"] = time.time()
        rec["ignored"] = 0
        _log(rec, 0.0, "开始生闷气")
        _save(data)
    return rec


def should_ignore(cid: str, text: str) -> tuple[bool, str]:
    """这条消息要不要不理。返回 (不理吗, 原因)。"""
    body = (text or "").strip()
    if any(w in body for w in URGENT_WORDS):
        return False, "急事"
    if body.count("在吗") >= 2:
        return False, "连问两次"

    if not is_sulking(cid):
        return False, ""
    if _affection(cid) < SULK_LEVEL:
        return False, "关系不够，只冷淡不闹"

    rec = _mark_sulk_start(cid)
    if int(rec.get("ignored") or 0) >= MAX_IGNORE:
        return False, "已经忽略了够多条"
    if time.time() - float(rec.get("sulk_at") or 0) > IGNORE_WINDOW:
        return False, "闹够 40 分钟了"
    return True, "生闷气"


def mark_ignored(cid: str) -> None:
    data = _load()
    rec = _rec(data, cid)
    rec["ignored"] = int(rec.get("ignored") or 0) + 1
    rec["sulk_at"] = rec.get("sulk_at") or time.time()
    _save(data)


def prompt_block(cid: str) -> str:
    """塞进提示词的情绪说明。"""
    rec = _rec(_load(), cid)
    mood = float(rec.get("mood") or 0)
    if mood > 1.5:
        return "（你现在心情很好，说话会比平时黏一点、暖一点。）"
    if mood > 0.4:
        return "（你现在心情不错。）"
    if mood >= -0.4:
        return ""
    if mood > SULK_THRESHOLD:
        return "（你现在有点小不高兴，说话短一点、别主动撒娇，但也没到闹别扭的地步。）"
    if not is_sulking(cid):
        return "（你不太开心，但不想闹，就当没事。）"
    return (
        "（你正在生闷气。说话要短、要冲一点，比如「哦」「嗯」「随便你」，"
        "不要主动找话题、不要撒娇、不要问他在干嘛。"
        "他要是道歉或者哄你，就顺着台阶下来，别一直顶着，也别装没事。"
        "如果他说有急事，立刻恢复正常口气，别闹了。）"
    )


def decay_all() -> int:
    """每天跑一次：情绪往 0 回，过期的闹别扭清掉。"""
    data = _load()
    today = time.time()
    touched = 0
    for cid, rec in list(data.items()):
        mood = float(rec.get("mood") or 0)
        if abs(mood) < 0.05 and not rec.get("sulk_at"):
            continue
        # 往 0 靠 0.5
        if mood > 0:
            rec["mood"] = round(max(0.0, mood - 0.5), 1)
        elif mood < 0:
            rec["mood"] = round(min(0.0, mood + 0.5), 1)
        started = float(rec.get("sulk_at") or 0)
        if started and today - started > FORGET_AFTER:
            rec["sulk_at"] = 0.0
            rec["ignored"] = 0
            rec["mood"] = max(float(rec.get("mood") or 0), -1.0)
            _log(rec, 0.0, "自己消气了")
        data[cid] = rec
        touched += 1
    if touched:
        _save(data)
    return touched


def report(cid: str) -> str:
    rec = _rec(_load(), cid)
    mood = float(rec.get("mood") or 0)
    sulking = is_sulking(cid)
    if mood > 1.5:
        state = "心情很好"
    elif mood > 0.4:
        state = "还不错"
    elif mood >= -0.4:
        state = "平静"
    elif mood > SULK_THRESHOLD:
        state = "有点小不高兴"
    elif sulking:
        state = "在生闷气"
    else:
        state = "不太开心，但没闹"

    lines = [f"她现在：{state}（情绪 {mood:+.1f}，范围 -3 ~ +3）"]
    if sulking:
        left = MAX_IGNORE - int(rec.get("ignored") or 0)
        lines.append(f"还在闹别扭：最多还能不理你 {max(0, left)} 条、或者再过 "
                     f"{max(0, int(IGNORE_WINDOW - (time.time() - float(rec.get('sulk_at') or 0))) // 60)} 分钟就自己消气")
        lines.append("哄法：说句软话、道个歉，或者干脆陪她聊会儿")
    events = (rec.get("why") or [])[-4:]
    if events:
        lines.append("最近的情绪变化：")
        for e in events:
            d = float(e.get("d") or 0)
            lines.append(f"· {e['ts']} {e['why']}" + (f" {d:+g}" if d else ""))
    else:
        lines.append("还没有情绪变化记录")
    return "\n".join(lines)
