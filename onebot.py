"""OneBot v11 接线：HTTP 上报收事件 + HTTP API 发消息。"""

import asyncio
import json
import logging
import re

import aiohttp

log = logging.getLogger("whale.onebot")

CQ_RE = re.compile(r"\[CQ:([a-z0-9_.-]+)((?:,[^\]]*)?)\]", re.IGNORECASE)
_UNESCAPE = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))

# 非文字消息段统一折叠成可读占位符
PLACEHOLDERS = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情包]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "forward": "[聊天记录]",
    "share": "[链接]",
    "music": "[音乐]",
}


class OneBotError(RuntimeError):
    pass


def _unescape(text: str) -> str:
    for src, dst in _UNESCAPE:
        text = text.replace(src, dst)
    return text


def extract_text(message, self_id=None) -> str:
    """把 array 或 CQ 字符串格式的消息转成纯文本。"""
    if message is None:
        return ""

    if isinstance(message, str):
        def _repl(match: re.Match) -> str:
            ctype = match.group(1).lower()
            params: dict[str, str] = {}
            for item in match.group(2).lstrip(",").split(","):
                if "=" in item:
                    k, _, v = item.partition("=")
                    params[k] = v
            if ctype == "text":
                return _unescape(params.get("text", ""))
            if ctype == "at":
                qq = params.get("qq", "")
                return "" if str(qq) == str(self_id) else f"@{qq}"
            return PLACEHOLDERS.get(ctype, "")
        return _unescape(CQ_RE.sub(_repl, message))

    if isinstance(message, list):
        parts: list[str] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "text":
                parts.append(str(data.get("text", "")))
            elif stype == "at":
                qq = str(data.get("qq", ""))
                if qq != str(self_id):  # @机器人本身不放进正文
                    parts.append(f"@{data.get('name') or qq}")
            elif stype == "reply":
                continue  # 引用气泡不进正文
            else:
                parts.append(PLACEHOLDERS.get(stype, ""))
        return "".join(parts)

    return str(message)


def mentioned_self(message, self_id) -> bool:
    """消息里有没有 @ 机器人。"""
    sid = str(self_id)
    if isinstance(message, list):
        return any(
            isinstance(seg, dict)
            and seg.get("type") == "at"
            and str((seg.get("data") or {}).get("qq", "")) == sid
            for seg in message
        )
    if isinstance(message, str):
        return "[CQ:at" in message and f"qq={sid}" in message
    return False


def images_all_stickers(message) -> bool:
    """消息里的图片是不是全是表情包（QQ 的 sub_type=1）。"""
    flags: list[bool] = []

    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict) or seg.get("type") != "image":
                continue
            data = seg.get("data") or {}
            raw = data.get("sub_type", data.get("subType", 0))
            flags.append(str(raw) == "1")
    elif isinstance(message, str):
        for match in CQ_RE.finditer(message):
            if match.group(1).lower() != "image":
                continue
            params = match.group(2) or ""
            flags.append("sub_type=1" in params or "subType=1" in params)

    return bool(flags) and all(flags)


def extract_images(message) -> list[str]:
    """把消息里的图片链接抠出来。array 和 CQ 字符串两种格式都支持。"""
    urls: list[str] = []

    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict) or seg.get("type") != "image":
                continue
            data = seg.get("data") or {}
            for key in ("url", "file"):
                value = data.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    urls.append(_unescape(value))
                    break

    elif isinstance(message, str):
        for match in CQ_RE.finditer(message):
            if match.group(1).lower() != "image":
                continue
            params: dict[str, str] = {}
            for item in match.group(2).lstrip(",").split(","):
                if "=" in item:
                    k, _, v = item.partition("=")
                    params[k] = v
            for key in ("url", "file"):
                value = params.get(key, "")
                if value.startswith("http"):
                    urls.append(_unescape(value))
                    break

    # 去重但保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def text_message(text: str, quote_id=None) -> list[dict]:
    """拼一条消息段数组，可选先引用某条消息。"""
    segments: list[dict] = []
    if quote_id is not None:
        segments.append({"type": "reply", "data": {"id": str(quote_id)}})
    segments.append({"type": "text", "data": {"text": text}})
    return segments


def voice_message(path, base64_encode: bool = True, quote_id=None) -> list[dict]:
    """拼一条语音消息。

    base64 方式能绕开路径和权限问题：NapCat 拿到的是一段数据，不需要去磁盘读文件。
    """
    import base64 as _b64
    from pathlib import Path as _Path

    file_path = _Path(path)
    if base64_encode:
        source = "base64://" + _b64.b64encode(file_path.read_bytes()).decode()
    else:
        source = str(file_path)

    segments: list[dict] = []
    if quote_id is not None:
        segments.append({"type": "reply", "data": {"id": str(quote_id)}})
    segments.append({"type": "record", "data": {"file": source}})
    return segments


def image_message(path, base64_encode: bool = True) -> list[dict]:
    """拼一条图片消息（用来发表情包）。"""
    import base64 as _b64
    from pathlib import Path as _Path

    file_path = _Path(path)
    if base64_encode:
        source = "base64://" + _b64.b64encode(file_path.read_bytes()).decode()
    else:
        source = str(file_path)
    return [{"type": "image", "data": {"file": source}}]


class OneBotClient:
    """调用 NapCat 暴露的 OneBot HTTP API。"""

    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token
        self._session: aiohttp.ClientSession | None = None

    async def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30), headers=headers
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def call(self, action: str, **params):
        session = await self._s()
        url = f"{self.base}/{action}"
        try:
            async with session.post(url, json=params) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise OneBotError(f"{action} HTTP {resp.status}：{body[:200]}")
        except aiohttp.ClientError as exc:
            raise OneBotError(
                f"{action} 连不上 {self.base}（NapCat 的「HTTP 服务器」开了吗？端口对吗？）：{exc}"
            ) from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return body

        status = data.get("status")
        retcode = data.get("retcode")
        if (status is not None and status != "ok") or (retcode is not None and retcode != 0):
            raise OneBotError(f"{action} 失败：{body[:300]}")
        return data.get("data")

    async def send_private(self, user_id, message):
        return await self.call("send_private_msg", user_id=int(user_id), message=message)

    async def send_group(self, group_id, message):
        return await self.call("send_group_msg", group_id=int(group_id), message=message)

    async def publish_qzone(self, content: str) -> str:
        """发一条 QQ 空间说说，返回 tid。

        注意：这个号必须先开通过 QQ 空间（在手机 QQ 里进去一次、或发过一条），
        否则 NapCat 会报 getClientKey Error。
        """
        data = await self.call("send_qzone_msg", content=content)
        if isinstance(data, dict):
            return str(data.get("tid") or "")
        return ""

    async def set_friend_add_request(
        self, flag: str, approve: bool = True, remark: str = ""
    ) -> dict:
        """同意/拒绝别人的加好友申请。flag 来自上报的 request 事件。"""
        params = {"flag": flag, "approve": bool(approve)}
        if remark:
            params["remark"] = remark[:20]
        return await self.call("set_friend_add_request", **params)

    async def get_login_info(self):
        return await self.call("get_login_info")

    async def get_friend_list(self) -> list:
        """好友列表。用来核对"刚通过的那个人是谁"。"""
        try:
            data = await self.call("get_friend_list")
        except OneBotError:
            return []
        return data if isinstance(data, list) else []

    async def get_status(self):
        """健康巡检用：返回 QQ 是否在线，连不上会抛 OneBotError。"""
        data = await self.call("get_status")
        return bool((data or {}).get("online"))

    async def fetch_image(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        """把图片下载下来，返回 (二进制, MIME)。超过上限就拒绝，别把内存吃满。"""
        session = await self._s()
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=25)
            ) as resp:
                if resp.status != 200:
                    raise OneBotError(f"图片下载失败 HTTP {resp.status}")
                mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0]
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise OneBotError(
                            f"图片超过 {max_bytes // 1024 // 1024}MB，跳过"
                        )
                    chunks.append(chunk)
                return b"".join(chunks), mime
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise OneBotError(f"图片下载出错：{exc}") from exc
