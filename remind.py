"""一次性定时：他说"今晚 11 点 20 分给我发条消息"，就真的到时候发。

和 `rules.py` 的分工：
- `rules.py` 管**重复性**约束（每天九点前别找我、别问吃了没）
- 这里管**一次性**约定（今晚 23:20 提醒我）

为什么单独一个文件：定时是一次性事件，要落盘、要能被后台循环取走、要能标记完成，
跟"规则"不是一个生命周期。混在一起会越写越乱。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import config

PATH = config.DATA_DIR / "reminders.json"

# 定时最多排到多久以后（防"明年今天提醒我"这种把文件撑爆的需求）
MAX_AHEAD_DAYS = 30


def _load() -> list:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list) -> None:
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _stamp(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def add(cid: str, at: float, note: str = "", raw: str = "",
        repeat: str = "once") -> dict:
    """排一条定时。返回这条记录。"""
    now = time.time()
    if at < now - 60:
        at += 86400          # 说的时间已经过了，按明天算
    if at > now + MAX_AHEAD_DAYS * 86400:
        at = now + MAX_AHEAD_DAYS * 86400
    items = _load()
    rec = {"cid": cid, "at": at, "note": (note or "")[:80], "raw": (raw or "")[:80],
           "repeat": repeat if repeat in ("once", "daily", "weekdays") else "once",
           "done": False, "created": now}
    items.append(rec)
    _save(items)
    return rec


def pending(cid: str = "") -> list:
    items = [r for r in _load() if not r.get("done")]
    if cid:
        items = [r for r in items if r.get("cid") == cid]
    return sorted(items, key=lambda r: r.get("at") or 0)


def due(now: float | None = None, grace: int = 600) -> list:
    """到点的定时（超过 grace 秒还没发的就算了，别隔天补发吓人）。"""
    now = now or time.time()
    out = []
    for r in pending():
        at = float(r.get("at") or 0)
        if now >= at and now - at <= grace:
            out.append(r)
    return out


def mark_done(rec: dict) -> None:
    items = _load()
    for r in items:
        if (r.get("cid") == rec.get("cid") and r.get("at") == rec.get("at")
                and r.get("created") == rec.get("created")):
            r["done"] = True
            r["done_at"] = time.time()
    _save(items)


def clear(cid: str = "") -> int:
    """清掉定时。不传 cid 就全清。"""
    items = _load()
    if not cid:
        _save([])
        return len(items)
    keep = [r for r in items if r.get("cid") != cid]
    _save(keep)
    return len(items) - len(keep)


def nearest_at(at: str, now: float | None = None) -> float:
    """把 "HH:MM" 解释成**最近的那个未来时间点**。

    为什么需要这个（踩过）：他 23:25 说「11点30叫我一下」，意思是 5 分钟后。
    按字面排 "今天 11:30"（已过去）→ 代码 +1 天 → 变成明天上午 11:30，人就被放鸽子了。
    正确做法是在 "今天 11:30 / 今天 23:30 / 明天 11:30 / 明天 23:30" 里挑最近的未来点。
    """
    now = now or time.time()
    hh, mm = int(at[:2]), int(at[3:])
    lt = time.localtime(now)
    cands = []
    for day_off in (0, 1):
        base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + day_off,
                            hh, mm, 0, 0, 0, -1))
        cands.append(base)              # 这个点本身（可能理解为上午）
        # 只有 hh<=12 才是"上午/晚上"两种解释；
        # hh>12（比如 23:30）再加 12 小时会跑到第二天 11:30，反而变成更近的错误候选（踩过）
        if hh <= 12:
            cands.append(base + 12 * 3600)
    future = [c for c in cands if c > now + 30]
    return min(future) if future else now + 3600


def next_occurrence(at: float, repeat: str) -> float:
    """下一次该发的时间。once 返回 0（不再排）。"""
    if repeat == "daily":
        nxt = at + 86400
    elif repeat == "weekdays":
        nxt = at + 86400
        while time.localtime(nxt).tm_wday >= 5:      # 跳过周六周日
            nxt += 86400
    else:
        return 0.0
    return nxt


def reschedule(rec: dict) -> bool:
    """重复型的定时发完之后，排下一次。"""
    repeat = rec.get("repeat") or "once"
    if repeat == "once":
        return False
    nxt = next_occurrence(float(rec.get("at") or 0), repeat)
    if not nxt:
        return False
    add(str(rec.get("cid")), nxt, str(rec.get("note") or ""),
        str(rec.get("raw") or ""), repeat)
    return True


def human_delta(at: float, now: float | None = None) -> str:
    """把"距离到点还有多久"说成人话。"""
    now = now or time.time()
    d = at - now
    if d < 0:
        return "已经过了"
    mins = int(d // 60)
    if mins < 1:
        return "不到 1 分钟"
    if mins < 60:
        return f"{mins} 分钟后"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours} 小时 {mins} 分钟后" if mins else f"{hours} 小时后"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时后" if hours else f"{days} 天后"


def describe(cid: str) -> str:
    rows = pending(cid)
    if not rows:
        return ""
    parts = []
    for r in rows[:4]:
        note = r.get("note") or "按约好发一条"
        tag = {"daily": "（每天）", "weekdays": "（工作日）"}.get(r.get("repeat") or "", "")
        parts.append(f"{_stamp(float(r['at']))}{tag} {note}")
    return "；".join(parts)


def report(cid: str) -> str:
    rows = pending(cid)
    if not rows:
        return "现在没有约好的定时消息。"
    lines = ["约好要发的消息："]
    for r in rows[:6]:
        note = r.get("note") or "（没说内容，随便说一句）"
        tag = {"daily": "（每天）", "weekdays": "（工作日）"}.get(r.get("repeat") or "", "")
        lines.append(
            f"· {_stamp(float(r['at']))}{tag} —— {note}"
            f"（{human_delta(float(r['at']))}）"
        )
    lines.append("要取消：/事件 清空")
    return "\n".join(lines)
