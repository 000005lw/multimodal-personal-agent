"""翻唱曲库。

提前做好的歌存在这儿，点歌时优先查它——这样不用开着家里电脑也能唱。
走中继现场生成的歌，成功后也会自动存一份进来，下次就秒回。
"""

import json
import logging
import re
import time
import uuid

from whale import config

log = logging.getLogger("whale.songs")

DIR = config.DATA_DIR / "songs"
INDEX = DIR / "index.json"

# 匹配时忽略这些
_STRIP = re.compile(r"[《》〈〉「」『』\"'“”‘’()（）\[\]【】{}\s\-_·、,，.。!！?？:：;；/\\]")
_TAIL = re.compile(r"(翻唱|cover|原唱|完整版|片段|无损|高音质)$")


def norm(text: str) -> str:
    """把歌名归一化，好做模糊匹配。"""
    value = _STRIP.sub("", (text or "").strip().lower())
    return _TAIL.sub("", value)


def load() -> dict:
    try:
        if INDEX.exists():
            data = json.loads(INDEX.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError) as exc:
        log.warning("曲库索引读不出来：%s", exc)
    return {}


def save(index: dict) -> None:
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        INDEX.write_text(
            json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("曲库索引保存失败：%s", exc)


def add_segment(
    query: str,
    title: str,
    index: int,
    audio: bytes,
    ext: str = "mp3",
    total: int | None = None,
) -> dict:
    """收一首歌的某一段进库。同一首歌可以存多段，电脑关着也能整首放。"""
    if not looks_like_song(title):
        log.warning("不存《%s》进曲库：标题看着不是歌", str(title)[:30])
        return {"query": query, "title": title, "segments": {}, "rejected": True}
    DIR.mkdir(parents=True, exist_ok=True)
    key = norm(query) or norm(title) or f"song{int(time.time())}"
    name = f"{uuid.uuid4().hex[:12]}.{ext}"
    (DIR / name).write_bytes(audio)

    data = load()
    entry = data.get(key) or {"query": query, "title": title, "segments": {}}
    segs = entry.get("segments")
    if not isinstance(segs, dict):
        # 老数据只有单文件，迁移成第 0 段
        segs = {}
        if entry.get("file"):
            segs["0"] = {"file": entry["file"], "ext": entry.get("ext", "mp3")}
    segs = dict(segs)

    old = segs.get(str(index))
    if old and old.get("file") and old["file"] != name:
        try:
            (DIR / old["file"]).unlink()
        except OSError:
            pass

    segs[str(index)] = {"file": name, "ext": ext}
    entry["segments"] = segs
    entry["query"] = entry.get("query") or query
    entry["title"] = entry.get("title") or title
    entry["ts"] = int(time.time())
    # 兼容老代码：file 字段永远指向第 0 段
    entry["file"] = segs.get("0", {}).get("file", entry.get("file"))
    entry["ext"] = segs.get("0", {}).get("ext", ext)
    if total:
        entry["total"] = int(total)

    data[key] = entry
    save(data)
    log.info(
        "曲库 +1：《%s》第 %d 段（共 %d 首）",
        str(title)[:30],
        index + 1,
        len(data),
    )
    return entry


def add(query: str, title: str, audio: bytes, ext: str = "mp3") -> dict:
    """收一首歌进库（只有一段，老接口）。"""
    return add_segment(query, title, 0, audio, ext=ext, total=1)


def segments(entry: dict | None) -> dict:
    """这首歌库里有哪些段：{"0": {...}, "1": {...}}"""
    if not entry:
        return {}
    segs = entry.get("segments")
    if isinstance(segs, dict) and segs:
        return segs
    if entry.get("file"):
        return {"0": {"file": entry["file"], "ext": entry.get("ext", "mp3")}}
    return {}


def looks_like_song(title: str) -> bool:
    """这个标题像不像正经的歌。

    2026-10-30 踩过：点《蔷薇》时错误地选中了游戏解说，那一段还被存进曲库，
    之后每次点《蔷薇》都直接秒发解说——重启也没用，因为脏东西在库里。
    """
    try:
        from whale.media import sing
    except Exception:
        return True
    text = str(title or "")
    if not text:
        return False
    if sing._EPISODE_RE.search(text) or sing._KEYSHIFT_RE.search(text):
        return False
    low = text.lower()
    if any(w in low for w in sing.BAD_WORDS):
        return False
    return True


def find(query: str) -> dict | None:
    """点歌时找一首。先精确，再互相包含。"""
    key = norm(query)
    if not key:
        return None
    index = load()
    if key in index:
        entry = index[key]
        if not looks_like_song(entry.get("title")):
            log.warning("曲库里的《%s》看着不是歌（%s），这次不用它",
                        str(entry.get("title"))[:30], key)
            return None
        return index[key]

    # 互相包含：「起风了」和「起风了买辣椒也用券」
    best = None
    for name, entry in index.items():
        if len(name) < 2 or len(key) < 2:
            continue
        if key in name or name in key:
            if best is None or len(name) < len(best[0]):
                best = (name, entry)
    if best and not looks_like_song(best[1].get("title")):
        log.warning("曲库里的《%s》看着不是歌，这次不用它", str(best[1].get("title"))[:30])
        return None
    return best[1] if best else None


def path_of(entry: dict | None, index: int = 0):
    """某一段的文件路径。库里没有这一段就返回 None。"""
    segs = segments(entry)
    info = segs.get(str(index))
    if not info or not info.get("file"):
        return None
    target = DIR / info["file"]
    return target if target.exists() and target.stat().st_size > 1024 else None


def total_of(entry: dict | None) -> int:
    """这首歌一共几段（不知道就是 0）。"""
    if not entry:
        return 0
    try:
        return int(entry.get("total") or 0)
    except (TypeError, ValueError):
        return 0


def listing() -> list[dict]:
    """给外面看的清单（不带文件路径）。"""
    index = load()
    return [
        {
            "query": entry.get("query"),
            "title": entry.get("title"),
            "ts": entry.get("ts"),
        }
        for entry in index.values()
    ]


def count() -> int:
    return len(load())


def names(limit: int = 0) -> list[str]:
    """曲库里的歌名（新入库的排前面）。limit=0 表示全部。"""
    index = load()
    ordered = sorted(index.values(), key=lambda e: e.get("ts") or 0, reverse=True)
    result = []
    for entry in ordered:
        name = str(entry.get("query") or entry.get("title") or "").strip()
        if name:
            result.append(name)
    return result[:limit] if limit else result


def mentioned(text: str) -> dict | None:
    """看这句话里有没有提到库里的歌名（长的优先，避免"晴天"吃掉"晴天娃娃"）。"""
    import re as _re

    if not text:
        return None
    blob = _re.sub(r"\s+", "", str(text))
    index = load()
    best = None
    best_len = 0
    for key, entry in index.items():
        name = norm(str(entry.get("query") or entry.get("title") or ""))
        if not name or len(name) < 2:
            continue
        if name in norm(blob) and len(name) > best_len:
            best = entry
            best_len = len(name)
    return best
