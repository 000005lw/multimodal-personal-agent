"""把分享链接解析成一句话，让她知道主人在看什么。

只做"读标题、作者、简介"这一层，不下视频、不看画面。
抖音那部分是在赌它的页面结构，会随版本变，所以任何异常都吞掉，退化成不解析。
"""

import json
import logging
import re
import shutil

import aiohttp

import config

log = logging.getLogger("whale.media")

URL_RE = re.compile(r"https?://[^\s，。！？、）)】\]\"'<>]+")
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"/av(\d+)", re.IGNORECASE)

UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

_session: aiohttp.ClientSession | None = None


async def _s() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": UA_MOBILE},
            # 配了代理就走代理：B站/抖音封机房 IP，住宅代理能过
            proxy=config.PROXY_URL or None,
        )
    return _session


async def close() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def find_urls(text: str) -> list[str]:
    urls = URL_RE.findall(text or "")
    # 去掉尾巴上常见的标点
    cleaned = []
    for url in urls:
        url = url.rstrip(".,;:!?)]}'\"")
        if url not in cleaned:
            cleaned.append(url)
    return cleaned


def _fmt_count(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


async def _get_text(url: str) -> tuple[str, str]:
    session = await _s()
    async with session.get(url, allow_redirects=True) as resp:
        return await resp.text(), str(resp.url)


BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


def _bili_headers() -> dict:
    """带登录态的请求头。B站的 412 常常是「没身份」，不是「封 IP」。"""
    headers = dict(BILI_HEADERS)
    if config.BILI_COOKIE:
        headers["Cookie"] = f"SESSDATA={config.BILI_COOKIE}"
    return headers


async def _bilibili(url: str) -> str | None:
    log.info("解析 B站链接：%s", url[:120])

    bvid = None
    aid = None
    match = BV_RE.search(url)
    if match:
        bvid = match.group(1)
    else:
        match = AV_RE.search(url)
        if match:
            aid = match.group(1)
        elif "b23.tv" in url:
            try:
                _, final = await _get_text(url)
                log.info("短链跳转到：%s", final[:120])
                match = BV_RE.search(final)
                if match:
                    bvid = match.group(1)
                else:
                    match = AV_RE.search(final)
                    aid = match.group(1) if match else None
            except OSError as exc:
                log.warning("B站短链跳转失败：%s", exc)
                return None

    if not bvid and not aid:
        log.info("B站链接里既没有 BV 也没有 av 号：%s", url[:120])
        return None

    session = await _s()

    # 第一层：官方接口
    query = f"bvid={bvid}" if bvid else f"aid={aid}"
    api = f"https://api.bilibili.com/x/web-interface/view?{query}"
    data: dict = {}
    try:
        async with session.get(api, headers=_bili_headers()) as resp:
            data = await resp.json(content_type=None)
    except (OSError, ValueError) as exc:
        log.warning("B站接口请求失败：%s", exc)

    if data.get("code") == 0:
        info = data["data"]
        duration = int(info.get("duration") or 0)
        minutes, seconds = divmod(duration, 60)
        return (
            f"B站视频《{info.get('title', '')}》，"
            f"UP主 {info.get('owner', {}).get('name', '')}，"
            f"时长 {minutes} 分 {seconds} 秒，"
            f"播放 {_fmt_count(int(info.get('stat', {}).get('view') or 0))}"
        )

    log.info(
        "B站接口返回 code=%s message=%s，改用网页标题兜底",
        data.get("code"),
        data.get("message"),
    )

    # 第二层：直接抓网页的 <title>
    page = f"https://www.bilibili.com/video/{bvid}" if bvid else f"https://www.bilibili.com/video/av{aid}"
    try:
        html, _ = await _get_text(page)
        found = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
        if found:
            title = re.sub(r"\s+", " ", found.group(1)).strip()
            for suffix in ("_哔哩哔哩_bilibili", "哔哩哔哩_bilibili"):
                title = title.replace(suffix, "")
            title = title.strip(" _-")
            # 被风控拦掉时返回的是「出错啦!」这种错误页，别当成视频标题
            if title and not any(bad in title for bad in ("出错啦", "啊哦", "404", "403")):
                log.info("B站网页标题兜底成功：%s", title[:40])
                return f"B站视频《{title}》（只知道标题）"
            if title:
                log.info("B站返回的是错误页（%s），放弃", title[:30])
    except OSError as exc:
        log.warning("B站网页兜底失败：%s", exc)

    return None


def _walk_douyin(node, depth: int = 0):
    """在 _ROUTER_DATA 里递归找带 desc 的那个节点。结构年年变，只能这样挖。"""
    if depth > 8:
        return None, None
    if isinstance(node, dict):
        desc = node.get("desc")
        author = None
        author_node = node.get("author")
        if isinstance(author_node, dict):
            author = author_node.get("nickname")
        if not author:
            author = node.get("nickname")
        if isinstance(desc, str) and desc.strip():
            return desc.strip(), author
        for value in node.values():
            found, found_author = _walk_douyin(value, depth + 1)
            if found:
                return found, found_author or author
    elif isinstance(node, list):
        for value in node:
            found, found_author = _walk_douyin(value, depth + 1)
            if found:
                return found, found_author
    return None, None


async def _douyin(url: str) -> str | None:
    try:
        html, final = await _get_text(url)
    except OSError:
        return None

    desc = author = None

    # 优先解析页面里的 _ROUTER_DATA：名字相对稳定，比逐字段正则抗变
    match = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S)
    if not match:
        match = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*\})", html, re.S)
    if match:
        try:
            desc, author = _walk_douyin(json.loads(match.group(1)))
        except (json.JSONDecodeError, ValueError):
            pass

    # 退回到粗暴的正则，能捞一个是一个
    if not desc:
        found = re.search(r'"desc"\s*:\s*"([^"]{1,150})"', html)
        if found:
            desc = found.group(1)
    if not author:
        found = re.search(r'"nickname"\s*:\s*"([^"]{1,40})"', html)
        if found:
            author = found.group(1)

    if not desc and not author:
        log.info("抖音页面没扒到标题：%s", final[:80])
        return None

    parts = ["抖音视频"]
    if desc:
        try:
            desc = json.loads(f'"{desc}"')
        except json.JSONDecodeError:
            pass
        parts.append(f"《{desc[:80]}》")
    if author:
        parts.append(f"作者 {author}")
    return "，".join(parts)


async def describe_one(url: str) -> str | None:
    """解析一条链接，返回一句话描述。解析不了就返回 None。"""
    try:
        if "bilibili.com" in url or "b23.tv" in url:
            return await _bilibili(url)
        if "douyin.com" in url or "iesdouyin.com" in url:
            return await _douyin(url)
    except (OSError, ValueError, KeyError) as exc:
        log.warning("链接解析失败 %s：%s", url[:60], exc)
    except Exception as exc:  # 页面结构随时在变，别让解析搞崩主流程
        log.warning("链接解析出意外 %s：%s", url[:60], exc)
    return None


async def describe_all(text: str, limit: int = 2) -> list[str]:
    urls = find_urls(text)[:limit]
    results = []
    for url in urls:
        info = await describe_one(url)
        if info:
            results.append(info)
    return results


def find_video_url(text: str) -> str | None:
    """找一条能下视频的链接。"""
    for url in find_urls(text):
        if any(
            host in url
            for host in ("bilibili.com", "b23.tv", "douyin.com", "iesdouyin.com")
        ):
            return url
    return None


# ---------------------------------------------------------------- 联网搜索

async def search(query: str, count: int = 5) -> list[dict]:
    """调 DeepSeek 自带的搜索接口。失败返回空列表，不影响正常聊天。"""
    if not config.API_KEY:
        return []
    payload = {
        "query": query[:120],
        "count": max(1, min(count, 10)),
        # 要正文：她能看到网页内容而不只是标题摘要
        "need_content": bool(config.SEARCH_WITH_CONTENT),
    }
    session = await _s()
    try:
        async with session.post(
            f"{config.BASE_URL}/v1/tools/search",
            json=payload,
            headers={
                "Authorization": f"Bearer {config.API_KEY}",
                "Content-Type": "application/json",
            },
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("搜索失败 HTTP %s：%s", resp.status, body[:200])
                return []
            data = await resp.json(content_type=None)
    except (OSError, ValueError) as exc:
        log.warning("搜索出错：%s", exc)
        return []
    return data.get("results") or []


def format_results(results: list[dict], limit: int = 5) -> str:
    lines = []
    for item in results[:limit]:
        title = (item.get("title") or "").strip()
        summary = (item.get("summary") or item.get("snippet") or "").strip()
        content = (item.get("content") or "").strip()
        date = (item.get("date") or "")[:10]
        head = f"- {title}"
        if date:
            head += f"（{date}）"
        body = summary or content
        if content and config.SEARCH_WITH_CONTENT:
            body = (summary + "\n" if summary else "") + content[
                : config.SEARCH_CONTENT_CHARS
            ]
        lines.append(head + ("：" + body if body else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- 抓图发图

MD_IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+?)\)")
BING_MURL = re.compile(r'murl&quot;:&quot;(https?://[^&]+?)&quot;')
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# 素材站、购物站、百科配图：搜冷门词时它们最容易被顶上来，但内容和用户要的无关
BAD_HOSTS = (
    "nipic.com", "699pic.com", "smzdm.com", "bcebos.com", "58pic.com",
    "51miz.com", "tukuppt.com", "zcool.com.cn", "vcg.com", "gettyimages",
    "shutterstock", "dreamstime", "123rf", "istockphoto", "taobao.com",
    "tmall.com", "jd.com", "amazon.", "alibaba.com", "made-in-china",
)


def _bad_image(url: str) -> bool:
    low = url.lower()
    return any(host in low for host in BAD_HOSTS)


async def fetch_markdown(url: str) -> str:
    """用 DeepSeek 的抓取接口把网页转成 Markdown。"""
    if not config.API_KEY:
        return ""
    session = await _s()
    try:
        # 抓取比普通请求慢得多，单独给 60 秒
        async with session.post(
            f"{config.BASE_URL}/v1/tools/fetch",
            json={"url": url},
            headers={
                "Authorization": f"Bearer {config.API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("抓取失败 HTTP %s：%s %s", resp.status, url[:60], body[:120])
                return ""
            data = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        log.warning("抓取超时：%s", url[:60])
        return ""
    except (OSError, ValueError) as exc:
        log.warning("抓取出错 %s：%s", url[:60], exc)
        return ""
    return data.get("content") or ""


async def _bing_images(query: str, want: int) -> list[str]:
    """必应图片搜索。实测一次能拿几十个地址，国内的服务器也访问得到。"""
    import html as htmlmod
    import urllib.parse

    url = "https://cn.bing.com/images/async?" + urllib.parse.urlencode(
        {
            "q": query[:60],
            "first": "1",
            "count": str(max(20, want * 6)),
            "mkt": "zh-CN",
        }
    )
    session = await _s()
    try:
        async with session.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Referer": "https://cn.bing.com/",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            timeout=aiohttp.ClientTimeout(total=25),
        ) as resp:
            body = await resp.text()
            log.info(
                "必应请求：status=%s 落地=%s 长度=%d",
                resp.status,
                str(resp.url)[:90],
                len(body),
            )
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("必应图片请求失败：%s", exc)
        return []

    found = BING_MURL.findall(body)
    urls: list[str] = []
    dropped = 0
    for item in found:
        clean = htmlmod.unescape(item)
        if _bad_image(clean):
            dropped += 1
            continue
        if clean not in urls:
            urls.append(clean)
    log.info(
        "必应图片「%s」拿到 %d 个地址（过滤掉素材站 %d 个）",
        query[:30],
        len(urls),
        dropped,
    )
    if not urls:
        log.info("必应返回的内容里没有可用图片，响应 %d 字节", len(body))
    return urls


async def find_images(query: str, want: int = 3) -> list[str]:
    """按关键词找图。

    只用必应图片。以前加了"搜网页再扒页面"的兜底，结果必应一失败就从不相关的
    页面里扒出几张无关图片发出去，比找不到更糟。所以兜底直接去掉。
    """
    query = (query or "").strip()
    if len(query) < 2:
        log.info("搜图：关键词太短（%r），放弃", query)
        return []

    urls = await _bing_images(query, want)
    if not urls:
        log.info("搜图：必应没返回结果「%s」", query[:30])
        return []
    return urls[: want * 6]


async def download_image(url: str):
    """下载一张图，太小或不是图片就丢掉。"""
    session = await _s()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            if resp.status != 200:
                return None
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0]
            if not ctype.startswith("image/"):
                return None
            raw = await resp.read()
    except OSError:
        return None

    if len(raw) < config.IMAGE_SEARCH_MIN_BYTES:
        return None  # 多半是图标或占位图

    if raw[:4] == b"\x89PNG":
        suffix = ".png"
    elif raw[:2] == b"\xff\xd8":
        suffix = ".jpg"
    elif raw[:4] == b"RIFF":
        suffix = ".webp"
    else:
        suffix = ".jpg"

    import uuid

    folder = config.DATA_DIR / "images"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"web_{uuid.uuid4().hex}{suffix}"
    out.write_bytes(raw)
    return out


# ---------------------------------------------------------------- 视频抽帧

FRAME_DIR = config.DATA_DIR / "frames"


def video_frames(url: str, count: int = 4, max_seconds: int = 180) -> list:
    """下视频、抽几帧出来。

    yt-dlp 和 ffmpeg 缺一个就直接放弃，不影响别的东西。
    """
    import shutil
    import subprocess
    import uuid

    ytdlp = shutil.which("yt-dlp")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not (ytdlp and ffmpeg and ffprobe):
        raise RuntimeError("缺 yt-dlp 或 ffmpeg，视频抽帧用不了")

    work = FRAME_DIR / uuid.uuid4().hex
    work.mkdir(parents=True, exist_ok=True)

    # 只下最低画质：反正后面还要缩到 512 宽，省带宽省时间
    try:
        subprocess.run(
            [
                ytdlp,
                "-f", "worst[ext=mp4]/worst",
                "--no-playlist",
                "--no-warnings",
                "--max-filesize", "60M",
                "-o", str(work / "v.%(ext)s"),
                url,
            ],
            capture_output=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("视频下载超时")

    videos = [p for p in work.iterdir() if p.suffix.lower() in (".mp4", ".flv", ".mkv", ".webm")]
    if not videos:
        raise RuntimeError("视频没下下来（可能需要登录，或这个站不支持）")
    video = videos[0]

    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, timeout=30,
    )
    try:
        duration = float(probe.stdout.decode().strip())
    except (ValueError, AttributeError):
        duration = 0.0
    if duration <= 0:
        raise RuntimeError("读不出视频时长")
    duration = min(duration, max_seconds)

    frames = []
    # 跳过开头，均匀取中间几个点
    points = [duration * (i + 1) / (count + 1) for i in range(count)]
    for index, second in enumerate(points):
        out = work / f"f{index}.jpg"
        subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-ss", f"{second:.2f}", "-i", str(video),
                "-frames:v", "1",
                "-vf", "scale=512:-2",
                str(out),
            ],
            capture_output=True, timeout=60, check=False,
        )
        if out.exists() and out.stat().st_size > 1000:
            frames.append(out)

    if not frames:
        raise RuntimeError("抽帧失败")
    _cleanup_frames()
    return frames


def _cleanup_frames(keep: int = 8) -> None:
    """每抽一次帧就建一个目录，留最近的几个，其余删掉免得占满磁盘。"""
    try:
        if not FRAME_DIR.exists():
            return
        dirs = sorted(
            (p for p in FRAME_DIR.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in dirs[keep:]:
            shutil.rmtree(stale, ignore_errors=True)
    except OSError:
        pass
