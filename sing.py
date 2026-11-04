"""自己找歌 → 分离人声 → 换成绘梨衣音色。

跑在家里那台 Windows 上（有显卡），由 home_agent 的 sing 任务调用。

流程：
    B站搜索 → 挑一个合适的视频 → yt-dlp 下音频
    → 截 60 秒 → demucs 抽人声 → RVC 换成 newvoice → mp3 回传
"""

import base64
import http.cookiejar
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# RVC 那套东西放哪（可用环境变量覆盖）
SING_DIR = Path(
    os.environ.get("WHALE_SING_DIR") or r"C:\Users\86173\Documents\whale-voice\2-唱歌"
)
COVER_PY = SING_DIR / "cover.py"

# 明显不是"一首歌"的标题，直接排掉
# 2026-10-29 补：有人点《雨爱》搜出个「当我忘记歌名时 我的搜索栏be like」的梗视频，
# 因为它 6 分钟、正好落进"像单曲"的时长区间，把真歌挤掉了。
BAD_WORDS = (
    "reaction", "dj", "慢速", "加速", "伴奏", "教学",
    "教程", "解说", "直播", "片段", "合集", "歌单", "循环", "纯音乐", "钢琴",
    "吉他", "指弹", "8d", "混音", "串烧", "remix",
    # 梗 / 搞笑 / 盘点类
    "搜索栏", "be like", "梗", "鬼畜", "空耳", "搞笑", "盘点", "排行", "榜单",
    "挑战", "认歌", "猜歌", "听歌识曲", "连音", "翻车", "复刻", "模仿", "对比",
    "铃声", "彩铃", "来电", "闹钟", "bgm", "背景音乐", "配乐",
    "教你", "怎么唱", "为什么", "解析", "测评", "盘点", "推荐",
    # 2026-10-30：点《蔷薇》被选中「【游戏王d】简评蔷薇龙补强」
    # —— 短歌名会撞上游戏/剧情类视频，这些词必须挡掉
    "简评", "攻略", "实况", "补强", "卡组", "游戏王", "对局", "卡牌",
    "剧场版", "预告", "花絮", "幕后", "第一集", "第2集", "剧集", "短剧",
    "动画", "混剪", "amv", "mad", "有声", "收音", "广播剧",
    # 2026-10-30 第三次踩：点了带歌手的《蔷薇》，搜出来的全是这类
    "原神", "任务", "世界任务", "寻找", "隐藏", "自用", "咬字", "降key",
    "降调", "key", "和弦", "扒谱", "乐理",
)

# 「降12key」「降 3 key」这种练声/分析视频
_KEYSHIFT_RE = re.compile(r"降\s*\d+\s*key|升\s*\d+\s*key", re.I)

# 标题里有这些，才像"一首歌"。短歌名（≤2 字）必须靠它们兜底。
MUSIC_WORDS = (
    "mv", "官方", "原唱", "歌曲", "单曲", "live", "现场", "演唱会",
    "hi-res", "hires", "无损", "录音棚", "纯享", "歌词", "lyrics",
    "专辑", "album", "音乐", "唱", "歌",
)

# 「第 8 集」这种明显是剧集，直接不要
_EPISODE_RE = re.compile(r"第\s*[0-9一二三四五六七八九十百]+\s*[集期话季章]")
GOOD_WORDS = ("原唱", "官方", "无损", "纯享", "完整版", "高音质", "hi-res", "hires",
              "录音棚", "试听", "4k")

# 命中这些词直接排除，不再只是扣分。
# 2026-10-30：43 分钟的「循环歌单」、DJ 1.2 倍速版、今日推荐类视频，
# 只靠 -8 分压不下去（时长分和音乐信号分一加就回到 0 分了）。
HARD_BAD = (
    "歌单", "循环", "合集", "串烧", "倍速", "升调", "降调", "变速", "dj",
    "remix", "8d", "伴奏", "教学", "教程", "解说", "直播", "简评", "攻略",
    "实况", "补强", "游戏王", "原神", "任务", "铃声", "彩铃", "教你",
    "解析", "测评", "盘点", "排行", "榜单", "混剪", "amv", "mad", "有声",
    "收音", "广播剧", "剧场版", "预告", "花絮", "幕后", "短剧", "剧集",
    "训诫剧", "围读", "有声剧", "情景剧", "微电影",
    "降key", "咬字", "和弦", "扒谱", "乐理", "混响", "鬼畜", "空耳",
    "搜索栏", "be like", "认歌", "猜歌", "听歌识曲",
)


# ---------- 搜索 ----------

def _opener():
    """先访问首页拿 buvid3，否则搜索接口直接 412。"""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", UA),
        ("Referer", "https://www.bilibili.com/"),
        ("Accept-Language", "zh-CN,zh;q=0.9"),
    ]
    try:
        with opener.open("https://www.bilibili.com/", timeout=20) as resp:
            resp.read(200)
    except Exception as exc:
        print(f"  [~] 拿 cookie 失败（先试试直接搜）：{type(exc).__name__}")
    return opener


def _seconds(stamp):
    """把 "5:27" / "130:1" 换成秒数，认不出来就返回 0。"""
    if not stamp:
        return 0
    try:
        total = 0
        for part in str(stamp).split(":"):
            total = total * 60 + int(part)
        return total
    except (TypeError, ValueError):
        return 0


def search(keyword, limit=20):
    """在 B站搜歌，返回候选列表。"""
    opener = _opener()
    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": 1,
        "page_size": limit,
    }
    url = (
        "https://api.bilibili.com/x/web-interface/search/type?"
        + urllib.parse.urlencode(params)
    )
    with opener.open(url, timeout=20) as resp:
        data = json.loads(resp.read())

    if data.get("code") != 0:
        raise RuntimeError(f"搜索失败 code={data.get('code')} {data.get('message')}")

    items = []
    for raw in (data.get("data") or {}).get("result") or []:
        bvid = raw.get("bvid") or ""
        if not bvid:
            continue
        title = re.sub(r"<[^>]+>", "", raw.get("title") or "").strip()
        items.append(
            {
                "title": title,
                "bvid": bvid,
                "url": f"https://www.bilibili.com/video/{bvid}",
                "duration": raw.get("duration") or "",
                "seconds": _seconds(raw.get("duration")),
                "author": raw.get("author") or "",
            }
        )
    return items


def _norm(text: str) -> str:
    """比标题用：去空白、标点、书名号，转小写。"""
    return re.sub(r"[\s，。！？、,.!?《》【】\[\]（）()\-~·'\"]+", "", (text or "").lower())


def pretty_title(title: str) -> str:
    """把 B 站视频标题收拾成能给用户看的歌名。

    「在百万豪装录音棚大声听林俊杰《修炼爱情》【Hi-res】」→「修炼爱情」
    （不收拾的话，她会一本正经地说"我唱的是《在百万豪装录音棚大声听…》"）
    """
    import re as _re

    raw = (title or "").strip()
    # B 站标题里常带 HTML 实体，直接显示会变成 &quot; 这种鬼东西
    raw = (raw.replace("&quot;", '"').replace("&amp;", "&")
              .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    m = _re.search(r"[《「]([^》」]{1,30})[》」]", raw)
    if m:
        return m.group(1).strip()
    out = _re.sub(r"[【\[][^】\]]{0,20}[】\]]", "", raw)
    out = _re.sub(
        r"(在百万豪装录音棚|大声听|Hi-?res|高音质|无损|完整版|官方|MV|字幕|纯享|4K|8K)",
        "", out, flags=_re.I,
    )
    out = out.strip(" -_·|、,，")
    return out or raw


def _standalone(name: str, text: str) -> bool:
    """歌名在标题里是不是**独立**出现的（不是更长词的一部分）。

    2026-10-30：之前用"歌名≤2字必须带 MV/歌曲 这类词"来防误配，
    结果把《蔷薇》-铁皮软糖的完整版（标题「【汐尔Sier】 蔷薇-铁皮软糖」）
    直接否掉了，只留下一个 1:15 的短片。真正该判的是"独立"：

        【汐尔Sier】 蔷薇-铁皮软糖      → 蔷薇 前后是空格和横线   ✅ 独立
        【游戏王dl】简评蔷薇龙补强       → 后面跟着"龙"            ✗ 不独立
        【自用】蔷薇骑士三种红色歌曲      → 后面跟着"骑士"          ✗ 不独立
        ...《野蔷薇》...              → 前面是"野"               ✗ 不独立（那是另一首）
    """
    if not name:
        return False

    def edge_ok(ch: str) -> bool:
        # 中文/字母/数字挨着就算"是别的词的一部分"
        return not ch or not ("\u4e00" <= ch <= "\u9fff" or ch.isalnum())

    start = 0
    while True:
        i = text.find(name, start)
        if i < 0:
            return False
        before = text[i - 1] if i > 0 else ""
        after = text[i + len(name)] if i + len(name) < len(text) else ""
        if edge_ok(before) and edge_ok(after):
            return True
        start = i + 1


def _score(item, keyword: str, require_name: bool = False) -> float:
    """给一个候选打分。require_name=True 时只认标题带歌名的。"""
    title = item["title"].lower()
    flat = _norm(item["title"])
    secs = item["seconds"]
    score = 0.0

    # 短歌名要用"独立出现"判——**必须在原始标题上判**：
    # _norm() 会把《》-空格全删掉，「《起风了》-买辣椒也用券」就粘成
    # 「起风了买辣椒也用券」，于是"独立"永远不成立（2026-10-30 踩过）
    if not keyword:
        has_name = False
    elif len(keyword) <= 4:
        has_name = _standalone(keyword, title) or _standalone(keyword, item["title"])
    else:
        has_name = keyword in flat or keyword in title
    if require_name and not has_name:
        return -99.0
    score += 10 if has_name else -8

    # 剧集类：第 8 集 / 第 2 期，直接否掉
    if _EPISODE_RE.search(item["title"]):
        return -99.0
    if _KEYSHIFT_RE.search(item["title"]):
        return -99.0
    if any(w in title for w in HARD_BAD):
        return -99.0
    # 不到 1 分钟的不可能是歌（实测搜"蔷薇"出来一堆 30~40 秒的切片）
    if secs and secs < 60:
        return -99.0
    # 超过 8 分钟的不是单曲（实测混进过 43 分钟的"循环歌单"）
    if secs and secs > 480:
        return -99.0

    music = any(w in title for w in MUSIC_WORDS)
    if music:
        score += 2
    # 长视频又没有音乐信号 → 基本是解说/剧情/综艺，不是歌
    if not music and secs > 360:
        score -= 12

    # 时长：整首歌一般 2~6 分钟。**越接近完整时长越优先**，
    # 否则 1 分多的试听/片段会和完整版打平（这次就输在平手上）。
    if 150 <= secs <= 360:
        score += 4
    elif 90 <= secs < 150:
        score += 3
    elif 60 <= secs < 90 or 360 < secs <= 420:
        score += 1
    else:
        score -= 4

    if any(word in title for word in GOOD_WORDS):
        score += 2
    if any(word in title for word in BAD_WORDS):
        score -= 8
    return score


def rank(items) -> list:
    """把候选按"像不像正经单曲"排好，**低于 0 分的直接丢掉**。

    2026-10-30：点《蔷薇》时列表里只有「第8集」和「【游戏王d】简评蔷薇龙补强」，
    旧逻辑在第一个下载失败后会退到"任意候选"，于是把游戏解说当歌发了。
    现在脏候选根本进不了这个列表，宁可返回空。
    """
    keyword = _norm((items[0].get("_keyword") if items else "") or "")
    scored = []
    for item in items:
        # 必须"标题里真有歌名"才进候选。
        # 否则像「整串『邓紫棋唱的泡沫』搜出来的无关视频」会靠时长+Hi-Res
        # 凑到 0 分混进来（2026-10-30 实测：它把《泡沫》换成了《寂寞星球的玫瑰》）
        sc = _score(item, keyword, require_name=True)
        if sc >= 0:
            scored.append((sc, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _sc, item in scored]


def pick(items):
    """从候选里挑一个最像正规单曲的（>=0 分里最高的）。挑不到返回 None。"""
    ranked = rank(items)
    if not ranked:
        kw = items[0].get("_keyword") if items else ""
        print(f"  [!] 没有一条像正经歌（点的是「{kw}」），这次不发")
        return None
    return ranked[0]


def split_artist_song(text: str, with_confidence: bool = False):
    """把「铁皮软糖唱的蔷薇」拆成 (歌手, 歌名)。

    为什么重要：整串拿去搜，标题里永远不可能出现「铁皮软糖唱的蔷薇」，
    匹配分全是 -8，直接判"没有像样的候选"。拆开之后：
    搜索用「铁皮软糖 蔷薇」（更准），**打分只按「蔷薇」**（能命中标题）。
    """
    import re as _re

    t = (text or "").strip().strip("《》「」\"' ")

    def _out(a: str, b: str, sure: bool):
        return (a, b, sure) if with_confidence else (a, b)

    if not t:
        return _out("", "", True)

    # ① 唱的 / 翻唱的 / 演唱的 / 版本的
    m = _re.match(r"^(.{1,14}?)(?:唱的|翻唱的|演唱的|版本的|唱)(.{2,40})$", t)
    if m:
        return _out(m.group(1).strip(), m.group(2).strip(), True)

    # ② 已经用空格分开的（"铁皮软糖 蔷薇"）
    if " " in t:
        head, _, tail = t.partition(" ")
        if len(head) >= 2 and len(tail) >= 2:
            return _out(head.strip(), tail.strip(), True)

    # ③ 「邓紫棋的红蔷薇白玫瑰」——**不确定**：可能是「下雨的夜晚」这种歌名本身带"的"
    m = _re.match(r"^(.{2,6}?)的(.{2,40})$", t)
    if m:
        return _out(m.group(1).strip(), m.group(2).strip(), False)

    return _out("", t, True)


def find_song(query: str):
    """搜歌 + 排序。**把几种拆法全都试一遍**，第一个搜到干净候选的就算数。

    2026-10-30 踩过：说「铁皮软糖的蔷薇」，`X的Y` 被标成"低把握"，
    于是只按整串搜 → 搜不到 → 报"没找到正经投稿"。
    用户直觉是"你两种都试试不就知道了"，那就都试。
    """
    artist, song, sure = split_artist_song(query, with_confidence=True)
    full = (query or "").strip()

    # (搜索词, 用来判断"标题含不含歌名"的关键词, 是否要求标题带歌手)
    attempts = [(full, full, False)]
    if artist and song and song != full:
        # ① 歌手 + 歌名（最准）
        attempts.append((f"{artist} {song}", song, True))
        # ② 只要歌名
        attempts.append((song, song, False))
        # ③ 歌名 + 后缀再搜两轮
        attempts.append((f"{song} MV", song, False))
        attempts.append((f"{song} 歌曲", song, False))
    else:
        low = full.lower()
        if full and not any(w in low for w in ("mv", "官方", "歌曲", "原唱", "live")):
            attempts.append((f"{full} MV", full, False))
            attempts.append((f"{full} 歌曲", full, False))

    if artist and song:
        print(f"  [·] 听出歌手「{artist}」，歌名「{song}」")

    seen = set()
    for used, keyword, need_artist in attempts:
        if used in seen:
            continue
        seen.add(used)
        items = search(used)
        if not items:
            print(f"  [!] 搜「{used}」没有结果")
            continue
        for it in items:
            it["_keyword"] = keyword
        ranked = rank(items)
        if need_artist:
            ranked = [it for it in ranked if artist in it["title"]]
            if not ranked:
                print(f"  [!] 「{used}」里没有标题带「{artist}」的")
                continue
        if ranked:
            if used != full:
                print(f"  [~] 用「{used}」搜到 {len(ranked)} 条像歌的")
            return ranked, used
        print(f"  [!] 搜「{used}」的 {len(items)} 个结果都不像歌，换一种搜法")
    return [], full


# ---------- 下载 / 处理 ----------

def _run(cmd, cwd=None, timeout=900, env=None):
    done = subprocess.run(
        cmd, cwd=cwd, timeout=timeout, capture_output=True, check=False, env=env
    )
    return (
        done.returncode,
        (done.stdout or b"").decode("utf-8", "replace"),
        (done.stderr or b"").decode("utf-8", "replace"),
    )


def download_audio(url, work, ffmpeg, timeout=300):
    """只下音频流，不转格式。

    不能用 -x/--audio-format：那一步要 ffprobe，而 imageio 带的 ffmpeg 里没有。
    """
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        raise RuntimeError("没装 yt-dlp")
    ffmpeg_dir = os.path.dirname(ffmpeg)
    env = dict(os.environ)
    env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")

    target = os.path.join(work, "song.%(ext)s")
    code, _, err = _run(
        [
            ytdlp,
            "-f", "ba/b",          # 只要音频流，下成原样（多半是 m4a）
            "--no-playlist",
            "--no-warnings",
            "--ffmpeg-location", ffmpeg_dir,
            "--max-filesize", "150M",
            "-o", target,
            url,
        ],
        timeout=timeout,
        env=env,
    )
    found = [
        os.path.join(work, n)
        for n in os.listdir(work)
        if n.startswith("song.") and not n.endswith(".part")
    ]
    if not found:
        detail = err.strip().splitlines()
        raise RuntimeError(
            "音频没下下来：" + (detail[-1][:160] if detail else f"code={code}")
        )
    return max(found, key=os.path.getsize)


def cut(audio, out, ffmpeg, start=15, seconds=60):
    """从原始音频里截一段出来，顺便转成 wav（44.1k 单声道）。"""
    code, _, err = _run(
        [
            ffmpeg, "-y",
            "-ss", str(start),
            "-i", audio,
            "-t", str(seconds),
            "-ar", "44100", "-ac", "1",
            out,
        ],
        timeout=300,
    )
    if not os.path.exists(out) or os.path.getsize(out) < 4096:
        # 起点可能超过总长度，从头再截一次
        code, _, err = _run(
            [
                ffmpeg, "-y", "-i", audio,
                "-t", str(seconds),
                "-ar", "44100", "-ac", "1",
                out,
            ],
            timeout=300,
        )
    if not os.path.exists(out) or os.path.getsize(out) < 4096:
        detail = err.strip().splitlines()
        raise RuntimeError("截取失败：" + (detail[-1][:160] if detail else f"code={code}"))
    return out


def separate_vocals(wav, work, ffmpeg=None, timeout=900):
    """demucs 抽人声。有显卡就用显卡。

    一定要把 imageio 那个 ffmpeg 塞进 PATH：demucs 内部用 torchaudio
    解码音频，找不到 ffmpeg 会报 "FFmpeg is not installed"（踩过）。
    """
    env = dict(os.environ)
    if ffmpeg:
        env["PATH"] = os.path.dirname(ffmpeg) + os.pathsep + env.get("PATH", "")

    out_dir = os.path.join(work, "sep")
    for device in ("cuda", "cpu"):
        code, out, err = _run(
            [
                "python", "-m", "demucs",
                "--two-stems=vocals",
                "-n", "htdemucs",
                "-d", device,
                "--out", out_dir,
                wav,
            ],
            timeout=timeout,
            env=env,
        )
        if code == 0:
            stem = Path(out_dir) / "htdemucs" / Path(wav).stem / "vocals.wav"
            if stem.exists():
                return str(stem)
        print(f"  [~] demucs({device}) 不行：{err.strip().splitlines()[-1][:120] if err else code}")
    raise RuntimeError("人声分离失败")


# 绘梨衣参考音频的主频中位数（实测，见 1-说话/voices/newvoice/ref-24k.wav）
# 变调目标就是把人声的主频挪到这个高度附近，咬字才清楚。
REFERENCE_F0 = 235.0
# 变调上限，别把女声也硬拉上去
PITCH_MIN, PITCH_MAX = -6, 7


# 往下降超过这个数就不要降了：
# 模型是女声模型，女声原调直接唱最好听；
# 硬压 6 个半音（练《宝船》那次）会变成又闷又抖的怪声音（2026-10-30 用户实测）。
DOWN_LIMIT = -2


def decide_shift(median: float) -> int:
    """根据人声主频算变调半音数（纯函数，好测）。"""
    import math

    if not median or median <= 0:
        return 0
    semitones = int(round(12 * math.log2(REFERENCE_F0 / median)))
    semitones = max(PITCH_MIN, min(PITCH_MAX, semitones))
    if semitones < DOWN_LIMIT:
        return 0
    return semitones


def detect_pitch_shift(vocals, max_seconds=25):
    """算出要升几个半音，才能让人声落进绘梨衣的音域。

    不猜男女——猜错过（周杰伦 198Hz 被判成女声）。改成量出这段人声的
    主频中位数，再算它和参考音高差几个半音。
    """
    try:
        import librosa
        import numpy as np

        data, rate = librosa.load(vocals, sr=22050, mono=True, duration=max_seconds)
        if data is None or len(data) < rate:
            return 0

        f0 = librosa.yin(data, fmin=65, fmax=600, sr=rate)
        rms = librosa.feature.rms(y=data)[0]
        # 只在有声音的帧上统计，静音段的 yin 会给一堆乱值
        size = min(len(f0), len(rms))
        mask = rms[:size] > np.percentile(rms[:size], 60)
        voiced = f0[:size][mask]
        voiced = voiced[np.isfinite(voiced)]
        if voiced.size == 0:
            return 0

        median = float(np.median(voiced))
        raw_shift = int(round(12 * float(np.log2(REFERENCE_F0 / median))))
        skipped = raw_shift < DOWN_LIMIT
        semitones = decide_shift(median)
        print(
            f"  人声主频 {median:.0f} Hz，参考音高 {REFERENCE_F0:.0f} Hz"
            f" → 变调 {semitones:+d} 半音"
            + ("（原本要往下降，压坏音色，改成原调）" if skipped else "")
        )
        return semitones
    except Exception as exc:
        print(f"  [~] 判断音高失败（按 0 处理）：{type(exc).__name__}")
        return 0


def convert_to_voice(vocals, out, pitch=0, protect=0.6, timeout=900):
    """用 newvoice 模型把这段人声换掉。

    protect 越大越保护气声和高音（不容易沙哑），越小咬字越清楚。
    """
    if not COVER_PY.exists():
        raise RuntimeError(f"找不到 cover.py：{COVER_PY}")
    code, stdout, err = _run(
        ["python", str(COVER_PY), vocals, out, str(pitch), "rmvpe", str(protect)],
        cwd=str(SING_DIR),
        timeout=timeout,
    )
    if not os.path.exists(out) or os.path.getsize(out) < 2048:
        detail = (err or stdout).strip().splitlines()
        raise RuntimeError("转换失败：" + (detail[-1][:160] if detail else f"code={code}"))
    return out


def to_mp3(wav, out, ffmpeg, seconds=60):
    """压成 mp3。码率给足：64k 会把咬字压糊（踩过）。"""
    code, _, err = _run(
        [
            ffmpeg, "-y", "-i", wav,
            "-t", str(seconds),
            "-ac", "1", "-ar", "44100", "-b:a", "128k",
            out,
        ],
        timeout=180,
    )
    if not os.path.exists(out):
        raise RuntimeError("压 mp3 失败：" + err.strip().splitlines()[-1][:160])
    return out


def make_song(query, ffmpeg, seconds=60, skip=15):
    """一条龙：搜歌 → 下音频 → 抽人声 → 换音色 → 返回 mp3 的 base64。"""
    if not ffmpeg:
        return {"error": "这台机器上没有 ffmpeg", "stage": "init"}

    ranked, used = find_song(query)
    if not ranked:
        return {"error": f"没找到「{query}」这首歌的正经投稿", "stage": "search"}
    song = ranked[0]
    print(f"  选中：{song['title']}（{song['duration']}）")

    work = tempfile.mkdtemp(prefix="whale_sing_")
    try:
        audio = download_audio(song["url"], work, ffmpeg)
        print("  音频已下载")

        clip = cut(audio, os.path.join(work, "clip.wav"), ffmpeg, start=skip, seconds=seconds)
        print("  已截段")

        vocals = separate_vocals(clip, work)
        print("  人声已分离")

        pitch = detect_pitch_shift(vocals)
        voice = convert_to_voice(vocals, os.path.join(work, "voice.wav"), pitch=pitch, protect=0.6)
        print("  音色已转换")

        mp3 = to_mp3(voice, os.path.join(work, "preview.mp3"), ffmpeg, seconds)
        payload = base64.b64encode(Path(mp3).read_bytes()).decode("ascii")
        print(f"  完成，{len(payload)//1024} KB（base64）")

        return {
            "title": song["title"],
            "bvid": song["bvid"],
            "url": song["url"],
            "author": song["author"],
            "seconds": seconds,
            "audio": payload,
            "ext": "mp3",
        }
    except Exception as exc:
        print(f"  [!] 失败：{type(exc).__name__}: {exc}")
        return {
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "title": song.get("title") if song else "",
            "url": song.get("url") if song else "",
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------- 整首分段：先下整首、分离整首，再一段段换音色 ----------

_CACHE = {}                       # 歌名 -> 已下载/已分离的文件，省得每段都重下
_CACHE_TTL = 3 * 3600
_CACHE_DIR = Path(tempfile.gettempdir()) / "whale_sing_cache"


def _cache_key(query: str) -> str:
    key = re.sub(r"[^\w\u4e00-\u9fff]", "", query or "")[:40]
    return key or "song"


def probe_seconds(ffmpeg, path):
    """从 ffmpeg 的 stderr 里抠时长（没有 ffprobe）。"""
    if not ffmpeg:
        return 0.0
    code, _, err = _run([ffmpeg, "-i", path], timeout=60)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", err or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def prepare_song(query, ffmpeg):
    """下载整首 + 分离整首人声，结果缓存。返回 dict。"""
    key = _cache_key(query)
    cached = _CACHE.get(key)
    if (
        cached
        and time.time() - cached["ts"] < _CACHE_TTL
        and Path(cached["vocals"]).exists()
    ):
        print(f"  用缓存的人声（{cached['duration']:.0f} 秒）")
        cached.setdefault(
            "total",
            max(1, __import__("math").ceil((cached.get("duration") or 0.0) / SEGMENT_SECONDS)),
        )
        return cached

    work = _CACHE_DIR / key
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    ranked, used = find_song(query)
    if not ranked:
        raise RuntimeError(f"没找到「{query}」的正经投稿")
    # 一条投稿挂了（B站 503 / 要登录）不该让整首歌失败——但只能在"像歌的候选"里换。
    # 2026-10-30 踩过：旧代码退到任意候选，把「【游戏王d】简评蔷薇龙补强」当歌发了。
    order = ranked[:6]
    song, audio, tried = None, None, 0
    for cand in order[:6]:
        tried += 1
        try:
            got = download_audio(cand["url"], str(work), ffmpeg)
        except Exception as exc:
            print(f"  [!] 「{str(cand.get('title'))[:28]}」下载失败，换下一个：{str(exc)[:60]}")
            continue

        # 时长校验：有些投稿下了个残缺片段（实测 213 秒的歌只下到 109 秒），
        # 那种唱出来只有半首，宁可换下一个候选
        got_sec = probe_seconds(ffmpeg, got)
        want_sec = _seconds(str(cand.get("duration") or ""))
        if want_sec and got_sec and got_sec < want_sec * 0.75:
            print(
                f"  [!] 「{str(cand.get('title'))[:24]}」只下到 {got_sec:.0f} 秒"
                f"（标称 {want_sec:.0f} 秒），换下一个"
            )
            continue

        audio, song = got, cand
        break
    if not audio or not song:
        raise RuntimeError(f"音频没下下来（试了 {tried} 个候选）")

    print(f"  选中：{song['title']}（{song['duration']}）")
    duration = probe_seconds(ffmpeg, audio)
    print(f"  音频已下载，{duration:.0f} 秒")

    # 先整首转成 wav，再交给 demucs：直接喂 m4a 时 demucs 会因为找不到
    # 它自己那份 ffmpeg 而报 "FFmpeg is not installed"
    full_wav = os.path.join(str(work), "full.wav")
    code, _, err = _run(
        [ffmpeg, "-y", "-i", audio, "-ar", "44100", "-ac", "2", full_wav], timeout=600
    )
    if not os.path.exists(full_wav):
        raise RuntimeError("转 wav 失败：" + (err.strip().splitlines()[-1][:120] if err else ""))
    print("  已转成 wav")

    vocals = separate_vocals(full_wav, str(work), ffmpeg=ffmpeg)
    print("  整首人声已分离")

    pitch = detect_pitch_shift(vocals)
    entry = {
        "title": song["title"],
        "bvid": song["bvid"],
        "url": song["url"],
        "author": song["author"],
        "vocals": vocals,
        "duration": duration or 0.0,
        "total": max(1, math.ceil((duration or 0.0) / SEGMENT_SECONDS)),
        "pitch": pitch,
        "ts": time.time(),
    }
    _CACHE[key] = entry
    return entry


SEGMENT_SECONDS = 59


def make_segment(query, ffmpeg, index=0, seconds=SEGMENT_SECONDS):
    """做整首歌里的第 index 段（0 开始）。"""
    import math

    if not ffmpeg:
        return {"error": "这台机器上没有 ffmpeg", "stage": "init"}

    try:
        song = prepare_song(query, ffmpeg)
    except Exception as exc:
        print(f"  [!] 准备失败：{type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"[:200], "stage": "prepare"}

    total = max(1, math.ceil((song["duration"] or seconds) / seconds))
    index = max(0, min(int(index), total - 1))
    start = index * seconds

    work = Path(song["vocals"]).parent
    try:
        seg = cut(
            song["vocals"],
            str(work / f"seg{index}.wav"),
            ffmpeg,
            start=start,
            seconds=seconds,
        )
        print(f"  第 {index + 1}/{total} 段已切出（从 {start} 秒起）")

        voice = convert_to_voice(seg, str(work / f"seg{index}_voice.wav"), pitch=song["pitch"], protect=0.6)
        print("  音色已转换")

        mp3 = to_mp3(voice, str(work / f"seg{index}.mp3"), ffmpeg, seconds)
        payload = base64.b64encode(Path(mp3).read_bytes()).decode("ascii")
        print(f"  完成，{len(payload)//1024} KB（base64）")

        return {
            "title": song["title"],
            "bvid": song["bvid"],
            "url": song["url"],
            "author": song["author"],
            "index": index,
            "total": total,
            "seconds": seconds,
            "duration": song["duration"],
            "audio": payload,
            "ext": "mp3",
        }
    except Exception as exc:
        print(f"  [!] 第 {index + 1} 段失败：{type(exc).__name__}: {exc}")
        return {
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "title": song.get("title"),
            "index": index,
        }


def make_song(query, ffmpeg, seconds=60, skip=15):
    """老接口：只做开头一段（曲库用）。"""
    return make_segment(query, ffmpeg, index=0, seconds=seconds)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from home_agent import find_ffmpeg

    keyword = sys.argv[1] if len(sys.argv) > 1 else "起风了"
    result = make_song(keyword, find_ffmpeg())
    if result.get("audio"):
        out = Path(os.path.dirname(os.path.abspath(__file__))) / "sing_test.mp3"
        out.write_bytes(base64.b64decode(result["audio"]))
        print("已保存到", out)
    else:
        print("失败：", result.get("error"))
