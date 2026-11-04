"""表情包。

按情绪分目录放在 data/stickers/ 下面，比如：

    data/stickers/开心/001.gif
    data/stickers/无语/002.jpg
    data/stickers/生气/003.png

目录名就是情绪名，也是模型写 [[贴图:开心]] 时用的词。
一个目录里可以放任意多张，每次随机挑一张。
"""

import logging
import random
from pathlib import Path

import config

log = logging.getLogger("whale.stickers")

STICKER_DIR = config.DATA_DIR / "stickers"
SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _emotion_dirs() -> dict[str, list[Path]]:
    """扫目录，返回 {情绪: [图片路径...]}。"""
    if not STICKER_DIR.exists():
        return {}
    result: dict[str, list[Path]] = {}
    for child in STICKER_DIR.iterdir():
        if not child.is_dir():
            continue
        files = [
            p for p in sorted(child.iterdir()) if p.is_file() and p.suffix.lower() in SUFFIXES
        ]
        if files:
            result[child.name] = files
    return result


def available() -> list[str]:
    """当前有哪些情绪可用。没图的时候返回空列表。"""
    return sorted(_emotion_dirs().keys())


def pick(emotion: str) -> Path | None:
    """按情绪随机挑一张。情绪不存在就返回 None，不报错。"""
    if not config.STICKERS_ENABLED:
        return None
    groups = _emotion_dirs()
    files = groups.get(emotion.strip())
    if not files:
        # 模型可能写「开心」而目录叫「高兴」，做个模糊兜底
        for name, candidates in groups.items():
            if emotion.strip() and (emotion.strip() in name or name in emotion.strip()):
                files = candidates
                break
    if not files:
        return None
    return random.choice(files)


def random_emotion() -> str | None:
    """随便挑一个情绪名。用于模型没主动发表情包时的概率兜底。"""
    emotions = available()
    return random.choice(emotions) if emotions else None
