"""Pure parsing for legacy model action markers.

This module deliberately has no side effects. It turns text such as
``[[贴图:开心]]`` into structured action dictionaries and returns the visible
text with control markers removed.
"""

from __future__ import annotations

import re


STICKER_RE = re.compile(r"\[\[(?:贴图|表情)[:：]\s*([^\[\]]{1,12})\]\]")
SEARCH_RE = re.compile(r"\[\[搜索[:：]\s*([^\[\]]{1,40})\]\]")
SCREENSHOT_RE = re.compile(r"\[\[截图\]\]")
DRAW_RE = re.compile(r"\[\[画[:：]\s*([^\[\]]{1,120})\]\]")
FIND_IMAGE_RE = re.compile(r"\[\[找图[:：]\s*([^\[\]]{1,40})\]\]")
SONG_RE = re.compile(r"\[\[唱[:：]\s*([^\[\]]{1,40})\]\]")
WATCH_RE = re.compile(r"\[\[看片\]\]")
VOICE_RE = re.compile(r"\[\[语音\]\]")
SONGLIST_RE = re.compile(r"\[\[歌单\]\]")
INTROSPECT_RE = re.compile(r"\[\[查[:：]\s*([^\[\]]{1,20})\]\]")


# Order is only a tie-breaker for markers at the same character offset.
ACTION_SPECS = (
    ("search", SEARCH_RE, True),
    ("screenshot", SCREENSHOT_RE, False),
    ("draw", DRAW_RE, True),
    ("find_image", FIND_IMAGE_RE, True),
    ("song", SONG_RE, True),
    ("watch_video", WATCH_RE, False),
    ("voice", VOICE_RE, False),
    ("songlist", SONGLIST_RE, False),
    ("introspect", INTROSPECT_RE, True),
    ("sticker", STICKER_RE, True),
)

_SPECIAL_PREFIXES = (
    "贴图", "表情", "搜索", "截图", "画", "找图", "唱",
    "语音", "歌单", "查", "看片",
)
_RESIDUAL_RE = re.compile(
    r"\[\[(?:"
    + "|".join(re.escape(item) for item in _SPECIAL_PREFIXES)
    + r")[^\]]*\]\]"
)
_UNCLOSED_RE = re.compile(
    r"\[\[(?:"
    + "|".join(re.escape(item) for item in _SPECIAL_PREFIXES)
    + r")[\s\S]*$"
)


def strip_action_markers(text: str) -> str:
    """Remove every known action marker, including malformed leftovers."""
    cleaned = text or ""
    for _, pattern, _ in ACTION_SPECS:
        cleaned = pattern.sub("", cleaned)
    cleaned = _RESIDUAL_RE.sub("", cleaned)
    cleaned = _UNCLOSED_RE.sub("", cleaned)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return "\n".join(lines).strip()


def extract_actions(
    raw_text: str,
    *,
    message_id: str | int | None = None,
    source: str = "model",
    allow_actions: bool = True,
) -> dict:
    """Return structured actions and marker-free visible text.

    The function never executes anything. When ``allow_actions`` is false,
    markers are still removed from visible text and returned in ``dropped`` so
    callers can trace actions emitted by second-pass or rewrite generations.
    """
    text = raw_text or ""
    matches: list[tuple[int, int, str, str, str]] = []

    for order, (kind, pattern, takes_arg) in enumerate(ACTION_SPECS):
        for match in pattern.finditer(text):
            arg = match.group(1).strip() if takes_arg else ""
            matches.append((match.start(), order, kind, arg, match.group(0)))

    # A valid marker nested inside a malformed control marker must not escape
    # as an executable action. The malformed outer marker will be scrubbed from
    # visible text below.
    valid_spans = {
        (start, start + len(marker))
        for start, _, _, _, marker in matches
    }
    nested = set()
    for residual in _RESIDUAL_RE.finditer(text):
        span = (residual.start(), residual.end())
        if span in valid_spans:
            continue
        for index, (start, _, _, _, marker) in enumerate(matches):
            if residual.start() <= start and start + len(marker) <= residual.end():
                nested.add(index)
    if nested:
        matches = [
            item for index, item in enumerate(matches)
            if index not in nested
        ]

    matches.sort(key=lambda item: (item[0], item[1]))

    prefix = str(message_id) if message_id not in (None, "") else "local"
    counters: dict[str, int] = {}
    actions: list[dict] = []
    for _, _, kind, arg, marker in matches:
        counters[kind] = counters.get(kind, 0) + 1
        action = {
            "id": f"{prefix}:{kind}:{counters[kind]}",
            "kind": kind,
            "arg": arg,
            "raw": marker,
            "source": str(source),
        }
        actions.append(action)

    visible = text
    for start, order, kind, arg, marker in reversed(matches):
        end = start + len(marker)
        visible = visible[:start] + visible[end:]
    visible = strip_action_markers(visible)

    if allow_actions:
        return {"actions": actions, "text": visible, "dropped": []}

    dropped = [
        {
            **action,
            "reason": "actions_disabled_for_rewrite",
        }
        for action in actions
    ]
    return {"actions": [], "text": visible, "dropped": dropped}
