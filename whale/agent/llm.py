"""DeepSeek 对话封装（OpenAI 兼容 /chat/completions）。"""

import asyncio
import json
import logging

import aiohttp

from whale import config

log = logging.getLogger("whale.llm")

_session: aiohttp.ClientSession | None = None


class LLMError(RuntimeError):
    """调用模型失败，消息可以直接给用户看。"""


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        headers = {}
        if config.API_KEY:
            headers["Authorization"] = f"Bearer {config.API_KEY}"
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120), headers=headers
        )
    return _session


async def close() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def chat(
    messages: list[dict],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    model: str | None = None,
) -> str:
    """发一轮对话，返回模型文本。失败抛 LLMError。"""
    if config.MOCK_LLM:
        last = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict)
                    )
                last = content
                break
        return f"离线自测模式：我听到你说：{last[:60]}"

    url = f"{config.BASE_URL}/chat/completions"
    payload = {
        "model": model or config.MODEL,
        "messages": messages,
        "temperature": config.TEMPERATURE if temperature is None else temperature,
        "max_tokens": config.MAX_TOKENS if max_tokens is None else max_tokens,
        "stream": False,
    }

    last_error: LLMError | None = None
    for attempt in range(3):
        try:
            session = await _get_session()
            async with session.post(url, json=payload) as resp:
                body = await resp.text()
                if resp.status == 200:
                    data = json.loads(body)
                    try:
                        choice = data["choices"][0]
                        content = (choice.get("message") or {}).get("content") or ""
                    except (KeyError, IndexError, TypeError) as exc:
                        raise LLMError(f"响应格式不对：{body[:300]}") from exc
                    if not content.strip():
                        raise LLMError(f"模型返回了空内容：{body[:300]}")
                    if choice.get("finish_reason") == "length":
                        log.warning("回复被 max_tokens 截断了")
                    return content.strip()

                if resp.status in (401, 403):
                    raise LLMError(
                        f"鉴权失败（{resp.status}），检查 DEEPSEEK_API_KEY 是否正确：{body[:200]}"
                    )
                if resp.status == 402:
                    raise LLMError("账户余额不足，去 DeepSeek 平台充值后再试。")
                if resp.status == 429 or resp.status >= 500:
                    last_error = LLMError(f"服务端返回 {resp.status}：{body[:200]}")
                    log.warning("第 %d 次尝试失败：%s", attempt + 1, last_error)
                else:
                    raise LLMError(f"请求失败 {resp.status}：{body[:300]}")

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            last_error = LLMError(f"网络连接失败：{exc}")
            log.warning("第 %d 次尝试网络异常：%s", attempt + 1, exc)

        await asyncio.sleep(1.2 * (attempt + 1))

    raise last_error or LLMError("未知错误")


# ---------------------------------------------------------------- 记忆压缩

_SUMMARY_SYSTEM = """你在为一个长期聊天的机器人整理「长期记忆」。

你的任务是把新发生的对话，合并进已有的摘要里，产出一份更新后的摘要。

规则：
- 只记录关于对方的事实、偏好、约定、正在做的事、没说完的话题、明显的情绪。
- 丢掉寒暄、重复内容、机器人自己的语气和口头禅。
- 用第三人称「对方」指称聊天对象，不要写成对话。
- 不要编造，没提到的不要猜。
- 输出纯文本，不要标题、不要列表符号、不要 markdown。
- 控制在字数上限内，宁可少写也不要注水。"""


async def summarize(old_summary: str, messages: list[dict], max_chars: int = 600) -> str:
    """把一段旧对话压成摘要，并与已有摘要合并。"""
    if not messages:
        return old_summary

    lines = []
    for msg in messages:
        who = "对方" if msg.get("role") == "user" else "小鲸鱼"
        content = (msg.get("content") or "").replace("\n", " ").strip()
        if content:
            lines.append(f"{who}：{content}")

    prompt = (
        f"已有的记忆摘要：\n{old_summary or '（还没有，这是第一次整理）'}\n\n"
        "新发生的对话：\n" + "\n".join(lines) + "\n\n"
        f"请输出合并后的新摘要，不超过 {max_chars} 字。"
    )

    result = await chat(
        [
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        # 给足输出预算，摘要上限调大时不至于被截断
        max_tokens=max(1200, max_chars * 2),
    )
    return result.strip()[: max_chars * 2]
