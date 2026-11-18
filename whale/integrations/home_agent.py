"""家里电脑中转客户端。

在你自己电脑上跑这个。它反过来去问云服务器"有活吗"，
拿到任务后用家里的宽带去读 B站/抖音（机房 IP 会被这些站拦，家里宽带不会）。

用法：
    1. 在 .env 里配好 HOME_TOKEN（和服务器 .env 里的 HOME_TOKEN 一样）
    2. 先开 SSH 隧道：ssh -N -L 8091:127.0.0.1:8090 ubuntu@YOUR_SERVER_IP
    3. 再跑：python home_agent.py
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

from whale import config

# 隧道把服务器上的 8090 映射到本机的 8091
BASE = os.environ.get("HOME_RELAY_URL", "http://127.0.0.1:8091").rstrip("/")
TOKEN = config.HOME_TOKEN.strip()

# 中继代码版本。改了 home_agent.py / sing.py 就 +1，
# 服务器靠它判断"对面那个黑窗口是不是还跑着旧代码"（忘了重启中继是常事）。
CODE_VERSION = 8

UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


def ensure_config() -> None:
    """Fail fast instead of sending unauthenticated relay requests."""
    if not TOKEN:
        raise RuntimeError(
            "缺少 HOME_TOKEN：请在 .env 中填入与服务器一致的 relay token"
        )


def http_get(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace"), str(resp.url)


def read_bilibili(url):
    headers = {
        "User-Agent": UA_DESKTOP,
        "Referer": "https://www.bilibili.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    bvid = None
    match = BV_RE.search(url)
    if match:
        bvid = match.group(1)
    elif "b23.tv" in url:
        _, final = http_get(url, headers)
        match = BV_RE.search(final)
        bvid = match.group(1) if match else None

    if not bvid:
        return None

    body, _ = http_get(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", headers
    )
    data = json.loads(body)
    if data.get("code") != 0:
        return None

    info = data["data"]
    duration = int(info.get("duration") or 0)
    minutes, seconds = divmod(duration, 60)
    # 顺便把简介带上，她聊得会更具体
    desc = (info.get("desc") or "").strip().replace("\n", " ")[:300]
    return (
        f"B站视频《{info.get('title', '')}》，"
        f"UP主 {info.get('owner', {}).get('name', '')}，"
        f"时长 {minutes}分{seconds}秒，"
        f"播放 {info.get('stat', {}).get('view', 0)}"
        + (f"，简介：{desc}" if desc else "")
    )


def read_douyin(url):
    headers = {"User-Agent": UA_MOBILE, "Referer": "https://www.douyin.com/"}
    html, _ = http_get(url, headers)
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
    desc = re.search(r'"desc"\s*:\s*"([^"]{1,150})"', html)
    author = re.search(r'"nickname"\s*:\s*"([^"]{1,40})"', html)
    parts = []
    if desc:
        parts.append(f"抖音视频《{desc.group(1)[:80]}》")
    if author:
        parts.append(f"作者 {author.group(1)}")
    if not parts and title:
        parts.append(f"抖音页面：{title.group(1).strip()[:80]}")
    return "，".join(parts) or None


def find_ffmpeg():
    """找 ffmpeg：PATH 里有就用，没有就用 imageio-ffmpeg 自带的那个。"""
    import shutil

    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def probe_duration(ffmpeg, video):
    """从 ffmpeg 的输出里抠出总时长。不用 ffprobe，少一个依赖。"""
    import re
    import subprocess

    done = subprocess.run(
        [ffmpeg, "-i", video],
        capture_output=True,
        timeout=60,
        check=False,
    )
    text = (done.stderr or b"").decode("utf-8", "replace")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def transcribe_audio(ffmpeg, video, token, appkey, max_minutes=10):
    """把视频的音轨转成文字（阿里云一句话识别，按 55 秒切块）。

    只上行音频、下行文字，不把整段音轨存到别处。
    """
    import json
    import math
    import os
    import subprocess
    import urllib.request

    audio = video + ".wav"
    subprocess.run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-i", video, "-vn", "-ac", "1", "-ar", "16000", audio,
        ],
        capture_output=True, timeout=300, check=False,
    )
    if not os.path.exists(audio):
        print("  [!] 抽音轨失败")
        return ""

    duration = probe_duration(ffmpeg, audio)
    total = min(duration or 0, max_minutes * 60)
    if total <= 0:
        print("  [!] 读不出音频长度")
        return ""

    chunk = 55
    pieces = max(1, math.ceil(total / chunk))
    print(f"  转写中：{total:.0f} 秒，分 {pieces} 段")

    texts = []
    for index in range(pieces):
        part = f"{audio}.{index}.wav"
        subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-ss", str(index * chunk), "-t", str(chunk),
                "-i", audio, "-ac", "1", "-ar", "16000", part,
            ],
            capture_output=True, timeout=120, check=False,
        )
        if not os.path.exists(part):
            continue
        try:
            with open(part, "rb") as handle_file:
                body = handle_file.read()
            url = (
                "https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/asr"
                f"?appkey={appkey}&format=wav&sample_rate=16000"
                "&enable_punctuation_prediction=true"
                "&enable_inverse_text_normalization=true"
            )
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "X-NLS-Token": token,
                    "Content-Type": "application/octet-stream",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            line = (data.get("result") or "").strip()
            if line:
                texts.append(line)
            print(f"    {index + 1}/{pieces} -> {len(line)} 字")
        except Exception as exc:
            print(f"    {index + 1}/{pieces} 失败：{type(exc).__name__} {exc}")
        finally:
            try:
                os.remove(part)
            except OSError:
                pass

    return "".join(texts)


def extract_frames(
    url, count=6, max_seconds=0, asr_token="", asr_appkey="", max_minutes=30
):
    """下载视频，均匀抽帧；有 token 就顺便转写音轨。"""

    import base64
    import os
    import shutil
    import subprocess
    import tempfile

    ytdlp = shutil.which("yt-dlp")
    ffmpeg = find_ffmpeg()
    if not (ytdlp and ffmpeg):
        print("  [!] 缺 yt-dlp 或 ffmpeg，抽不了帧")
        return []

    # yt-dlp 的 --download-sections 要自己找 ffmpeg，而 imageio 那个不在 PATH 里
    ffmpeg_dir = os.path.dirname(ffmpeg)
    env = dict(os.environ)
    env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")

    work = tempfile.mkdtemp(prefix="whale_")
    try:
        done = subprocess.run(
            [
                ytdlp,
                # B站的视频和音频是分开的两条流，让 yt-dlp 各取最差的再合并。
                # 画质故意压低：反正后面要缩到 512 宽。
                "-f", "wv*+wa/w/b",
                "--no-playlist",
                "--no-warnings",
                "--ffmpeg-location", ffmpeg_dir,
                # 不用 --download-sections：那条路还要 ffprobe，imageio 没带
                "--max-filesize", "120M",
                "-o", os.path.join(work, "v.%(ext)s"),
                url,
            ],
            capture_output=True,
            timeout=600,
            check=False,
            env=env,
        )

        found = [
            os.path.join(work, name)
            for name in os.listdir(work)
            if name.lower().endswith((".mp4", ".flv", ".mkv", ".webm"))
        ]
        if not found:
            detail = (done.stderr or b"").decode("utf-8", "replace").strip()
            print("  [!] 视频没下下来")
            if detail:
                for line in detail.splitlines()[-6:]:
                    print("      " + line[:160])
            # 把最后一行 ERROR 带回去，好让服务器知道到底为什么失败
            reason = ""
            for line in reversed(detail.splitlines()):
                if "ERROR" in line or "错误" in line:
                    reason = line.strip()[:160]
                    break
            return [], "", reason or "下载失败"
        video = found[0]

        # 没有 ffprobe，yt-dlp 不会合并音视频，会下成两个文件。
        # 这对我们反而方便：音频本来就是独立的一条。
        separate_audio = [
            os.path.join(work, name)
            for name in os.listdir(work)
            if name.lower().endswith((".m4a", ".aac", ".mp3", ".opus", ".ogg"))
        ]

        duration = probe_duration(ffmpeg, video)
        if duration <= 0:
            duration = 300.0
        if max_seconds and duration > max_seconds:
            duration = float(max_seconds)
        print(f"  video duration: {duration:.0f}s")

        # 从 5% 到 95% 均匀取点：只截开头的话，后面十几分钟她等于没看
        start = duration * 0.05
        end = duration * 0.95
        step = (end - start) / max(1, count - 1)
        points = [start + step * i for i in range(count)]

        frames = []
        for index, second in enumerate(points):
            out = os.path.join(work, f"s{index}.jpg")
            subprocess.run(
                [
                    ffmpeg, "-y", "-loglevel", "error",
                    "-ss", f"{second:.1f}",
                    "-i", video,
                    "-frames:v", "1",
                    "-vf", "scale=512:-2",
                    out,
                ],
                capture_output=True, timeout=60, check=False,
            )
            # 小于 4 KB 的多半是黑屏或纯色卡，留着也没用
            if os.path.exists(out) and os.path.getsize(out) > 4000:
                with open(out, "rb") as handle_file:
                    frames.append(base64.b64encode(handle_file.read()).decode())
        print(f"  -> 抽到 {len(frames)} 帧（全片均匀取样）")

        transcript = ""
        if asr_token and asr_appkey:
            try:
                source = separate_audio[0] if separate_audio else video
                transcript = transcribe_audio(
                    ffmpeg, source, asr_token, asr_appkey, max_minutes=max_minutes
                )
                print(f"  -> 转写 {len(transcript)} 字")
            except Exception as exc:
                print(f"  [!] 转写失败：{type(exc).__name__} {exc}")

        return frames, transcript, ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


def fetch_trending(limit=12):
    """B站热门视频列表。用来让她"自己挑一个看"。"""
    import json

    headers = {
        "User-Agent": UA_DESKTOP,
        "Referer": "https://www.bilibili.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    body, _ = http_get(
        f"https://api.bilibili.com/x/web-interface/popular?ps={limit}&pn=1", headers
    )
    data = json.loads(body)
    if data.get("code") != 0:
        print("  [!] 热门接口返回", data.get("code"), data.get("message"))
        return []

    items = []
    for item in (data.get("data") or {}).get("list") or []:
        title = (item.get("title") or "").strip()
        bvid = item.get("bvid") or ""
        owner = (item.get("owner") or {}).get("name") or ""
        if title and bvid:
            items.append(
                {
                    "title": title,
                    "owner": owner,
                    "url": f"https://www.bilibili.com/video/{bvid}",
                }
            )
    print(f"  -> 热门列表 {len(items)} 条")
    return items


def handle(job):
    kind = job.get("kind")
    url = job.get("url") or ""
    text = None
    try:
        if "bilibili.com" in url or "b23.tv" in url:
            text = read_bilibili(url)
        elif "douyin.com" in url:
            text = read_douyin(url)
    except Exception as exc:
        print(f"  [!] 读页面失败：{type(exc).__name__} {exc}")

    if kind == "trending":
        return {"items": fetch_trending()}

    if kind == "page":
        return text

    if kind == "video":
        frames, transcript, error = [], "", ""
        try:
            frames, transcript, error = extract_frames(
                url,
                asr_token=job.get("asr_token") or "",
                asr_appkey=job.get("asr_appkey") or "",
                max_minutes=int(job.get("asr_minutes") or 30),
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            print(f"  [!] 处理视频失败：{error}")
        return {
            "text": text,
            "frames": frames,
            "transcript": transcript,
            "error": error,
        }

    if kind == "sing":
        from whale.media import sing

        try:
            # 一定要读 job["segment"]：这是第几段。
            # 以前这里调的是 make_song（固定第 0 段），导致点几段都发第一段
            index = int(job.get("segment") or 0)
            print(f"  做第 {index + 1} 段")
            res = sing.make_segment(
                job.get("query") or url,
                find_ffmpeg(),
                index=index,
                seconds=int(job.get("seconds") or 59),
            )
            # 把"点的是什么"带回去：服务器补发时要靠它开续播会话（踩过）
            if isinstance(res, dict):
                res.setdefault("query", job.get("query") or url)
            return res
        except Exception as exc:
            print(f"  [!] 唱歌失败：{type(exc).__name__} {exc}")
            return {"error": f"{type(exc).__name__}: {exc}"[:200], "stage": "sing"}

    return None


def poll():
    ensure_config()
    url = f"{BASE}/jobs/poll?v={CODE_VERSION}"
    req = urllib.request.Request(
        url, headers={"X-Home-Token": TOKEN, "X-Home-Version": str(CODE_VERSION)}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def report(job_id, result):
    ensure_config()
    body = json.dumps({"id": job_id, "result": result}).encode()
    req = urllib.request.Request(
        f"{BASE}/jobs/result",
        data=body,
        headers={"Content-Type": "application/json", "X-Home-Token": TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def main():
    try:
        ensure_config()
    except RuntimeError as exc:
        print(f"[!] {exc}")
        return 2
    print("=" * 46)
    print("  家里电脑中转 已启动")
    print(f"  连的是 {BASE}")
    print("  这个窗口别关，关了她就借不到你的网了")
    print("=" * 46)
    fails = 0
    while True:
        try:
            data = poll()
            fails = 0
        except (urllib.error.URLError, OSError) as exc:
            fails += 1
            if fails in (1, 10, 50):
                print(f"[!] 连不上服务器（{exc}）。隧道通吗？")
            time.sleep(3)
            continue

        job = data.get("job")
        if not job:
            continue

        print(f"[活] {job.get('kind')} {str(job.get('url'))[:70]}")
        result = handle(job)
        if isinstance(result, dict):
            print(
                f"  -> 文字 {len(str(result.get('text') or ''))} 字，"
                f"{len(result.get('frames') or [])} 帧，"
                f"转写 {len(str(result.get('transcript') or ''))} 字"
            )
        else:
            print("  ->", (str(result)[:60] if result else "没读到"))
        try:
            report(job["id"], result)
        except (urllib.error.URLError, OSError) as exc:
            print("  [!] 交作业失败：", exc)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已停止")
