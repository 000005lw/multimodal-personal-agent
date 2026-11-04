"""她的情绪状态。

解决的问题：以前她没写 [[贴图:情绪]] 时，代码会 `random_emotion()` 随机挑一个
目录，于是出现"嘴上说好开心，发出来一张生气的图"。

现在每个情绪分类都带两个维度：

    valence（价度）：-1 很丧 ~ +1 很嗨
    arousal（唤醒）：0 平静 ~ 1 激动

她的心情是一个会衰减的值，表情包只从"和当前心情 + 这句话语气都说得通"的分类里挑。
如果模型自己写了 [[贴图:X]] 但和这句话的语气完全相反，也会被纠正掉。

状态存 data/emotion.json，重启不丢。
"""

import json
import logging
import random
import re
import time
from pathlib import Path

import config

log = logging.getLogger("whale.emotion")

STATE_FILE = config.DATA_DIR / "emotion.json"

# 半衰期：40 分钟没聊，心情往中性回落一半
HALF_LIFE = 40 * 60

# 每个表情包分类在这个坐标里的位置（valence, arousal）
CATEGORIES: dict[str, tuple[float, float]] = {
    "开心": (0.85, 0.60),
    "爱心": (0.90, 0.45),
    "摸头": (0.75, 0.20),
    "害羞": (0.45, 0.35),
    "求夸": (0.55, 0.50),
    "感谢": (0.60, 0.25),
    "得意": (0.65, 0.55),
    "乖巧": (0.50, 0.15),
    "馋": (0.30, 0.45),
    "好奇": (0.30, 0.45),
    "打招呼": (0.40, 0.30),
    "关注": (0.20, 0.30),
    "鲸鱼": (0.50, 0.20),
    "收到": (0.20, 0.20),
    "看书": (0.10, 0.10),
    "思考": (0.10, 0.30),
    "疑惑": (0.00, 0.45),
    "搞事": (0.30, 0.75),
    "搞怪": (0.05, 0.60),
    "无语": (-0.25, 0.15),
    "困": (-0.10, 0.05),
    "生气": (-0.65, 0.80),
    "委屈": (-0.60, 0.25),
    "难过": (-0.85, 0.20),
}

# 关键词 → 情绪。按顺序匹配，先中的算数。
HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("爱心", ("喜欢你", "爱你", "想你", "抱抱", "贴贴", "亲亲")),
    ("开心", ("哈哈", "开心", "好耶", "太好了", "高兴", "笑死", "有趣", "好玩")),
    ("害羞", ("害羞", "脸红", "不好意思", "别夸", "羞")),
    ("求夸", ("夸我", "厉害吧", "我棒", "表扬")),
    ("得意", ("嘿嘿", "当然", "必须", "那当然", "我可是")),
    ("馋", ("想吃", "好饿", "饿死", "馋", "吃饭", "泡面", "火锅")),
    ("困", ("好困", "困了", "睡觉", "晚安", "睡醒", "打哈欠", "累了", "好累")),
    ("委屈", ("委屈", "欺负", "凶我", "不理我")),
    ("难过", ("难过", "伤心", "呜呜", "哭", "失落", "不开心")),
    ("生气", ("生气", "烦死", "讨厌", "气死", "哼", "滚", "过分")),
    ("无语", ("无语", "服了", "算了", "随你", "受不了")),
    ("疑惑", ("为什么", "怎么会", "啥", "? ", "？", "不懂", "奇怪")),
    ("搞事", ("要不要", "整他", "搞事", "来点", "试试")),
    ("关注", ("看我", "注意", "在吗", "理我", "陪我")),
    ("思考", ("我想想", "让我想", "思考", "琢磨", "考虑")),
    ("打招呼", ("你好", "早安", "在不在", "哈喽", "hi")),
    ("感谢", ("谢谢", "谢啦", "感谢", "多亏")),
)


def _blank() -> dict:
    return {"valence": 0.0, "arousal": 0.3, "label": "平静", "ts": time.time()}


def _load() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("情绪状态读不出来：%s", exc)
        return {}


def _save(data: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        log.warning("情绪状态写不进去：%s", exc)


def infer(text: str) -> tuple[str, float, float]:
    """从一句话判断情绪，返回 (情绪名, valence, arousal)。中性返回 ("", 0, 0.3)。"""
    s = (text or "").strip()
    if not s:
        return "", 0.0, 0.3
    lowered = s.lower()
    for label, words in HINTS:
        for w in words:
            if w.lower() in lowered:
                v, a = CATEGORIES.get(label, (0.0, 0.3))
                return label, v, a
    return "", 0.0, 0.3


def reset(cid: str) -> None:
    """把这个人的心情清回中性（自测用，也方便你哪天让她"重新开始"）。"""
    data = _load()
    data.pop(cid, None)
    _save(data)


def current(cid: str) -> dict:
    """她此刻的心情（会按时间衰减）。"""
    data = _load()
    rec = data.get(cid) or _blank()
    elapsed = max(0.0, time.time() - float(rec.get("ts") or 0))
    if elapsed > 0:
        keep = 0.5 ** (elapsed / HALF_LIFE)
        rec = {
            "valence": float(rec.get("valence") or 0.0) * keep,
            "arousal": float(rec.get("arousal") or 0.3) * keep
            + 0.3 * (1 - keep),
            "label": rec.get("label") or "平静",
            "ts": time.time(),
        }
    return rec


def update(cid: str, text: str) -> str:
    """用她刚说的话更新心情。返回当前情绪名。"""
    label, v, a = infer(text)
    if not label:
        # 中性的话：情绪轻微松弛，但不改标签
        rec = current(cid)
        rec["valence"] *= 0.9
        rec["ts"] = time.time()
        data = _load()
        data[cid] = rec
        _save(data)
        return nearest_label(rec["valence"], rec["arousal"])

    rec = current(cid)
    # 新情绪占 45%，旧心情留 55%：不会一句翻脸，也不会积压
    rec["valence"] = rec["valence"] * 0.55 + v * 0.45
    rec["arousal"] = rec["arousal"] * 0.55 + a * 0.45
    rec["label"] = label
    rec["ts"] = time.time()

    data = _load()
    data[cid] = rec
    _save(data)
    return label


def nearest_label(valence: float, arousal: float) -> str:
    """坐标上最近的分类名。"""
    best, best_d = "平静", 1e9
    for name, (v, a) in CATEGORIES.items():
        d = (v - valence) ** 2 + (a - arousal) ** 2
        if d < best_d:
            best, best_d = name, d
    return best


def consistent(cid: str, text: str) -> str:
    """给一句话挑一个"情绪说得通"的分类。

    优先用这句话本身的情绪；看不出来就用当前心情；两者打架时取中间。
    """
    label, v, a = infer(text)
    rec = current(cid)
    mood_v, mood_a = rec["valence"], rec["arousal"]

    # 这句话有明确情绪、而且分类库里真有 → 优先就用它
    if label and label in CATEGORIES and random.random() < 0.75:
        return label

    # 中性的话 + 心情也平稳 → 不硬发，免得"嗯"也甩一张图
    if not label and abs(mood_v) < 0.25:
        return ""

    if label:
        # 这句话有明确情绪，但当前心情差太远时各取一半
        if abs(v - mood_v) > 1.0:
            v = (v + mood_v) / 2
            a = (a + mood_a) / 2
    else:
        v, a = mood_v, mood_a

    # 只在"同一大类"里挑：价度差超过 0.9 的分类一律不要
    candidates = [
        name
        for name, (cv, ca) in CATEGORIES.items()
        if abs(cv - v) <= 0.9 and abs(ca - a) <= 0.8
    ]
    if not candidates:
        return nearest_label(v, a)

    # 再按距离加权，离得近的概率高，但避免永远同一张脸
    scored = sorted(
        ((name, (CATEGORIES[name][0] - v) ** 2 + (CATEGORIES[name][1] - a) ** 2)
         for name in candidates),
        key=lambda kv: kv[1],
    )
    top = scored[:3]
    weights = [1.0 / (1.0 + d) for _, d in top]
    total = sum(weights)
    r = random.uniform(0, total)
    acc = 0.0
    for (name, _), w in zip(top, weights):
        acc += w
        if r <= acc:
            return name
    return top[0][0]


def clash(cid: str, text: str, folder: str) -> bool:
    """模型写的贴图分类和这句话严重矛盾吗。"""
    if folder not in CATEGORIES:
        return False
    _, v, a = infer(text)
    rec = current(cid)
    # 以"这句话的情绪"为准；看不出来就比心情
    ref_v = v if v else rec["valence"]
    cv, _ = CATEGORIES[folder]
    return abs(cv - ref_v) > 1.2


def describe(cid: str) -> str:
    """给提示词用的一句当前心情。"""
    rec = current(cid)
    v = rec["valence"]
    if v > 0.55:
        mood = "心情很好"
    elif v > 0.2:
        mood = "心情不错"
    elif v > -0.2:
        mood = "心情平稳"
    elif v > -0.55:
        mood = "有点低落"
    else:
        mood = "挺难过的"
    label = nearest_label(v, rec["arousal"])
    return f"你现在{mood}（偏「{label}」）"


def stats(cid: str) -> str:
    rec = current(cid)
    return (
        f"心情值 {rec['valence']:+.2f}（{nearest_label(rec['valence'], rec['arousal'])}）"
    )
