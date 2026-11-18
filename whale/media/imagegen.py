"""文生图 / 图生图：接阿里云百炼的通义万相。

两条路：
  t2i（wanx2.0-t2i-turbo）—— 从文字凭空画
  edit（wanx2.1-imageedit）—— 拿一张参考图当基准改，用来保持角色一致

画「她自己」时，如果配了参考图就走 edit，人物长相就固定了；
没配参考图才退回文字描述（那样每次脸都会变）。
"""

import asyncio
import base64
import logging
import uuid
from pathlib import Path

import aiohttp

from whale import config

log = logging.getLogger("whale.imagegen")

T2I_SUBMIT = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
)
EDIT_SUBMIT = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2image/image-synthesis"
)
TASK = "https://dashscope.aliyuncs.com/api/v1/tasks/{}"
IMAGE_DIR = config.DATA_DIR / "images"


class ImageError(RuntimeError):
    """生图失败，消息可以直接给用户看。"""


def _headers(async_mode: bool = False) -> dict:
    headers = {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    if async_mode:
        headers["X-DashScope-Async"] = "enable"
    return headers


async def _post_task(session: aiohttp.ClientSession, url: str, payload: dict) -> str:
    async with session.post(url, json=payload, headers=_headers(True)) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise ImageError(f"提交失败 HTTP {resp.status}：{body[:200]}")
        try:
            data = await resp.json(content_type=None)
        except ValueError as exc:
            raise ImageError(f"返回不是 JSON：{body[:200]}") from exc

    output = data.get("output") or {}
    task_id = output.get("task_id")
    if not task_id:
        message = data.get("message") or str(data)[:200]
        raise ImageError(f"没拿到 task_id：{message}")
    return task_id


def _strip_keys(prompt: str) -> str:
    extra = prompt
    for key in config.IMAGE_SELF_KEYS:
        extra = extra.replace(key, "")
    return extra.strip(" ，,。的把张画一只")


def _self_ref() -> Path | None:
    path = config.DATA_DIR / config.IMAGE_SELF_REF
    return path if path.exists() else None


async def _poll(session: aiohttp.ClientSession, task_id: str) -> str:
    loop = max(6, config.IMAGE_TIMEOUT // 3)
    for _ in range(loop):
        await asyncio.sleep(3)
        async with session.get(TASK.format(task_id), headers=_headers()) as resp:
            if resp.status != 200:
                continue
            data = await resp.json(content_type=None)
        output = data.get("output") or {}
        status = (output.get("task_status") or "").upper()
        if status == "SUCCEEDED":
            results = output.get("results") or []
            url = (results[0] or {}).get("url") if results else None
            if not url:
                raise ImageError("任务成功但没有图片地址")
            return url
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            detail = output.get("message") or str(output)[:200]
            raise ImageError(f"生成失败（{status}）：{detail}")
    raise ImageError(f"等了 {config.IMAGE_TIMEOUT} 秒还没出图，放弃了")


async def _download(session: aiohttp.ClientSession, url: str) -> Path:
    async with session.get(url) as resp:
        if resp.status != 200:
            raise ImageError(f"图片下载失败 HTTP {resp.status}")
        raw = await resp.read()
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    # 通义万相有时给 PNG 有时给 JPEG，按文件头判断，别瞎写扩展名
    if raw[:4] == b"\x89PNG":
        suffix = ".png"
    elif raw[:2] == b"\xff\xd8":
        suffix = ".jpg"
    else:
        suffix = ".png"
    out = IMAGE_DIR / f"{uuid.uuid4().hex}{suffix}"
    out.write_bytes(raw)
    return out


def _cleanup(keep: int = 30) -> None:
    try:
        if not IMAGE_DIR.exists():
            return
        files = sorted(
            (p for p in IMAGE_DIR.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[keep:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


async def generate(prompt: str, self_ref: bool | None = None) -> Path:
    """按描述生成一张图，返回本地文件路径。

    self_ref 为 True 时强制走参考图；None 表示从提示词里自动判断。
    """
    if not config.IMAGE_ENABLED:
        raise ImageError("生图功能没开")
    if not config.DASHSCOPE_API_KEY:
        raise ImageError(
            "没配 DASHSCOPE_API_KEY，去百炼控制台创建一个 API-KEY 填进 .env"
        )

    prompt = (prompt or "").strip()
    if not prompt:
        raise ImageError("没有描述，画不了")

    is_self = (
        any(key in prompt for key in config.IMAGE_SELF_KEYS)
        if self_ref is None
        else self_ref
    )
    scene = _strip_keys(prompt) if is_self else prompt
    ref = _self_ref() if is_self else None

    timeout = aiohttp.ClientTimeout(total=config.IMAGE_TIMEOUT + 30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if ref is not None:
            # 有参考图：锁住长相，只换场景和动作
            # description_edit 才会真的改动作；stylization_all 只换画风、姿势几乎不动
            edit_prompt = (
                "把画面改成："
                + (scene or "站着微笑")[:120]
                + "。人物还是原来那个：同一张脸、同样的深蓝色长发、同样的女仆装和鲸鱼围裙。"
                + "画风："
                + config.IMAGE_SELF_STYLE
            )
            payload = {
                "model": config.IMAGE_EDIT_MODEL,
                "input": {
                    "function": config.IMAGE_EDIT_FUNCTION,
                    "prompt": edit_prompt[:800],
                    "base_image_url": "data:image/jpeg;base64,"
                    + base64.b64encode(ref.read_bytes()).decode(),
                },
                "parameters": {"n": 1},
            }
            log.info("按参考图生成（%s）：%s", ref.name, edit_prompt[:60])
            task_id = await _post_task(session, EDIT_SUBMIT, payload)
        else:
            if is_self:
                # 没有参考图，只能靠文字描述，脸会变
                prompt = "，".join(
                    part
                    for part in (config.IMAGE_SELF_DESC, scene, config.IMAGE_SELF_STYLE)
                    if part
                )
            payload = {
                "model": config.IMAGE_MODEL,
                "input": {"prompt": prompt[:800]},
                "parameters": {"size": config.IMAGE_SIZE, "n": 1},
            }
            log.info("按文字生成：%s", prompt[:60])
            task_id = await _post_task(session, T2I_SUBMIT, payload)

        log.info("生图任务 %s 已提交", task_id)
        url = await _poll(session, task_id)
        path = await _download(session, url)

    log.info("生图完成：%s（%.0f KB）", path.name, path.stat().st_size / 1024)
    _cleanup()
    return path


def _cleanup(keep: int = 30) -> None:
    try:
        if not IMAGE_DIR.exists():
            return
        files = sorted(
            IMAGE_DIR.glob("*.jpg"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[keep:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass
