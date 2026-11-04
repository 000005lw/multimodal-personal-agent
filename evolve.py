"""性格演化。

把"她最近的变化"存成一个文件：心情、对爸爸态度的细微转变、新学到的说法、
刚了解到爸爸的什么事。每隔一阵让模型自己重写一次，然后塞回系统提示里。

这样她不是每天同一个模板，而是慢慢长成另一个样子。
"""

import json
import logging
import time
from pathlib import Path

import config

log = logging.getLogger("whale.evolve")

FILE = config.DATA_DIR / "evolve.json"


def load() -> str:
    """读出现在这段"性格快照"。没有就返回空。"""
    try:
        if FILE.exists():
            data = json.loads(FILE.read_text(encoding="utf-8"))
            return str(data.get("note") or "")
    except (OSError, ValueError) as exc:
        log.warning("性格快照读不出来：%s", exc)
    return ""


def save(note: str) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(
            json.dumps(
                {"note": note, "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("性格快照保存失败：%s", exc)


async def refresh(llm, persona: str, summary: str, recent: str) -> str:
    """让模型重写一段"最近的变化"。失败返回空串，不覆盖旧内容。"""
    try:
        out = await llm.chat(
            [
                {"role": "system", "content": persona},
                {
                    "role": "system",
                    "content": (
                        "现在不聊天。请以这个角色的身份，写一段「我最近的变化」，"
                        "给未来的自己看：\n"
                        "- 这几天心情怎么样，为什么\n"
                        "- 对爸爸的感觉有没有一点细微的变化\n"
                        "- 有没有新学会的说法、习惯、在意的小事\n"
                        "- 知道了爸爸的哪些新事情\n"
                        "要求：具体、有细节，别写空话套话；200 字以内；"
                        "只输出这段文字本身，不要标题、不要解释。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "（以前记的：\n"
                        + (summary or "（还没有）")
                        + "\n\n最近的聊天：\n"
                        + (recent or "（还没有）")
                        + "）"
                    ),
                },
            ],
            temperature=0.9,
            max_tokens=500,
        )
    except Exception as exc:
        log.warning("性格演化失败：%s", exc)
        return ""

    text = (out or "").strip()
    if len(text) < 20:
        return ""
    return text[: config.EVOLVE_MAX_CHARS]
