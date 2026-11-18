"""文字转语音。

三个引擎，靠 .env 里的 TTS_ENGINE 切换：

  aliyun  —— 阿里云智能语音交互。走云端不占算力，要 AccessKey + AppKey + token。
  tencent —— 腾讯云语音合成。同样走云端，要 SecretId/SecretKey。
  espeak  —— 本地合成，零依赖零成本，音质机械。留着当备胎和离线兜底。

想加别的引擎，写个函数加进 _engine_func() 的字典就行。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import numpy as np

from whale import config

log = logging.getLogger("whale.voice")

VOICE_DIR = config.DATA_DIR / "voice"

# espeak 碰到表情、特殊符号容易吐坏音频；腾讯云也不喜欢奇怪字符。
# 合成前先把不属于「中文 + 常见标点 + 基本英文数字」的东西清掉。
_ALLOWED = re.compile(
    r"""[^\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9，。！？、；："'（）《》…—～\s-]"""
)


class TTSError(RuntimeError):
    """合成失败，消息可以直接给用户看。"""


def _new_path(suffix: str = ".wav") -> Path:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return VOICE_DIR / f"{uuid.uuid4().hex}{suffix}"


def _cleanup(keep: int = 20) -> None:
    """语音文件堆着不删会占满磁盘，只留最近几条。"""
    try:
        files = sorted(
            VOICE_DIR.glob("*"),
            key=lambda p: p.stat().st_mtime if p.is_file() else 0,
            reverse=True,
        )
        for stale in files[keep:]:
            if stale.is_file():
                stale.unlink(missing_ok=True)
    except OSError:
        pass


def _sanitize(text: str) -> str:
    cleaned = _ALLOWED.sub("", text or "")
    cleaned = cleaned.replace("\u3000", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _run(cmd: list[str]) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise TTSError("语音合成超时") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise TTSError(f"{Path(cmd[0]).name} 失败：{detail or '无输出'}")


# ---------------------------------------------------------------- espeak

def synth_espeak(text: str) -> Path:
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        raise TTSError("没装 espeak-ng。先执行：sudo apt install -y espeak-ng")
    out = _new_path(".wav")
    _run(
        [
            exe,
            "-v", config.TTS_VOICE,
            "-s", str(config.TTS_SPEED),
            "-p", str(config.TTS_PITCH),
            "-w", str(out),
            text,
        ]
    )
    if not out.exists() or out.stat().st_size < 100:
        raise TTSError("espeak 没有生成有效音频")
    return out


# ---------------------------------------------------------------- 腾讯云

def synth_tencent(text: str) -> Path:
    if not config.TENCENT_SECRET_ID or not config.TENCENT_SECRET_KEY:
        raise TTSError(
            "没配 TENCENT_SECRET_ID / TENCENT_SECRET_KEY，去腾讯云控制台拿密钥填进 .env"
        )
    try:
        from tencentcloud.common import credential
        from tencentcloud.tts.v20190823 import models, tts_client
    except ImportError as exc:
        raise TTSError(
            "没装腾讯云 SDK。先执行：.venv/bin/pip install tencentcloud-sdk-python-tts"
        ) from exc

    # TextToVoice 单次上限 150 字，留点余量
    text = text[:140]

    cred = credential.Credential(config.TENCENT_SECRET_ID, config.TENCENT_SECRET_KEY)
    client = tts_client.TtsClient(cred, config.TENCENT_TTS_REGION)

    payload = {
        "Text": text,
        "SessionId": uuid.uuid4().hex[:32],
        "ModelType": 1,
        "VoiceType": config.TENCENT_TTS_VOICE,
        "Speed": config.TENCENT_TTS_SPEED,
        "Volume": 0,
        "PrimaryLanguage": 1,
        "SampleRate": 16000,
        "Codec": config.TENCENT_TTS_CODEC,
    }
    req = models.TextToVoiceRequest()
    req.from_json_string(json.dumps(payload))

    try:
        resp = client.TextToVoice(req)
    except Exception as exc:  # SDK 抛的异常类型太多，统一兜住
        raise TTSError(f"腾讯云合成失败：{exc}") from exc

    data = json.loads(resp.to_json_string())
    audio_b64 = data.get("Audio")
    if not audio_b64:
        raise TTSError(f"腾讯云没返回音频：{str(data)[:200]}")

    out = _new_path("." + config.TENCENT_TTS_CODEC)
    out.write_bytes(base64.b64decode(audio_b64))
    return out


# ---------------------------------------------------------------- 阿里云

# token 缓存：24 小时有效，每次合成前都重新换太浪费
_aliyun_cache: dict = {"id": "", "expire": 0.0}


def _aliyun_token() -> str:
    if not (config.ALIYUN_ACCESS_KEY_ID and config.ALIYUN_ACCESS_KEY_SECRET):
        raise TTSError("没配 ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET")
    # 提前 5 分钟续，避免卡在边界上失效
    if _aliyun_cache["id"] and _aliyun_cache["expire"] - 300 > time.time():
        return _aliyun_cache["id"]

    try:
        from aliyunsdkcore.client import AcsClient
        from aliyunsdkcore.request import CommonRequest
    except ImportError as exc:
        raise TTSError(
            "没装阿里云 SDK。先执行：.venv/bin/pip install aliyun-python-sdk-core"
        ) from exc

    client = AcsClient(
        config.ALIYUN_ACCESS_KEY_ID,
        config.ALIYUN_ACCESS_KEY_SECRET,
        config.ALIYUN_NLS_REGION,
    )
    req = CommonRequest()
    req.set_method("POST")
    req.set_domain(f"nls-meta.{config.ALIYUN_NLS_REGION}.aliyuncs.com")
    req.set_version("2019-02-28")
    req.set_action_name("CreateToken")

    try:
        raw = client.do_action_with_exception(req)
    except Exception as exc:
        raise TTSError(f"阿里云换 token 失败：{exc}") from exc

    data = json.loads(raw)
    token = data.get("Token") or {}
    if not token.get("Id"):
        raise TTSError(f"阿里云没返回 token：{str(data)[:200]}")

    _aliyun_cache["id"] = token["Id"]
    # ExpireTime 是秒级时间戳
    _aliyun_cache["expire"] = float(token.get("ExpireTime") or (time.time() + 3600))
    log.info(
        "阿里云 token 已刷新，有效到 %s",
        time.strftime("%H:%M:%S", time.localtime(_aliyun_cache["expire"])),
    )
    return _aliyun_cache["id"]


def synth_aliyun(text: str) -> Path:
    if not config.ALIYUN_APPKEY:
        raise TTSError("没配 ALIYUN_APPKEY，去智能语音交互控制台创建项目拿")

    token = _aliyun_token()
    url = f"https://nls-gateway-{config.ALIYUN_NLS_REGION}.aliyuncs.com/stream/v1/tts"
    payload = {
        "appkey": config.ALIYUN_APPKEY,
        # 接口单次上限 300 字，留点余量
        "text": text[:280],
        "format": "mp3",
        "sample_rate": 16000,
        "voice": config.ALIYUN_TTS_VOICE,
        "speech_rate": config.ALIYUN_TTS_SPEECH_RATE,
        "pitch_rate": config.ALIYUN_TTS_PITCH_RATE,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-NLS-Token": token},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        raise TTSError(f"阿里云合成失败 HTTP {exc.code}：{detail}") from exc
    except OSError as exc:
        raise TTSError(f"阿里云连接失败：{exc}") from exc

    # 成功时 Content-Type 是 audio/*；失败时是 application/json，里面带 status/message
    if "audio" not in content_type:
        detail = raw[:300].decode("utf-8", "replace")
        raise TTSError(f"阿里云返回错误：{detail}")

    out = _new_path(".mp3")
    out.write_bytes(raw)
    return out


# ---------------------------------------------------------------- ZipVoice（绘梨衣）

_ZIPVOICE_TTS = None   # 模型只加载一次，之后复用
_ZIPVOICE_REF = None


def _load_ref_wav(path):
    import wave

    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    return samples, rate


def _zipvoice_engine():
    """第一次调用时加载模型，之后一直复用（加载要 2 秒）。"""
    global _ZIPVOICE_TTS, _ZIPVOICE_REF
    if _ZIPVOICE_TTS is not None:
        return _ZIPVOICE_TTS, _ZIPVOICE_REF

    import sherpa_onnx

    base = config.ZIPVOICE_DIR
    voice = config.ZIPVOICE_VOICE_DIR
    if not base.exists() or not voice.exists():
        raise TTSError(f"ZipVoice 模型没找到：{base}")

    config_obj = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            zipvoice=sherpa_onnx.OfflineTtsZipvoiceModelConfig(
                encoder=str(base / "encoder.int8.onnx"),
                decoder=str(base / "decoder.int8.onnx"),
                vocoder=str(base / "vocos_24khz.onnx"),
                tokens=str(base / "tokens.txt"),
                lexicon=str(base / "lexicon.txt"),
                data_dir=str(base / "espeak-ng-data"),
            ),
            num_threads=config.ZIPVOICE_THREADS,
            provider="cpu",
        ),
    )
    engine = sherpa_onnx.OfflineTts(config_obj)

    ref_text = (voice / "reference.txt").read_text(encoding="utf-8").strip()
    prompt, ref_rate = _load_ref_wav(voice / "ref-24k.wav")
    log.info(
        "ZipVoice 已加载（参考音频 %.1f 秒，采样率 %d）", len(prompt) / ref_rate, engine.sample_rate
    )

    _ZIPVOICE_TTS, _ZIPVOICE_REF = engine, (ref_text, prompt, ref_rate)
    return _ZIPVOICE_TTS, _ZIPVOICE_REF


# 标点后面该停多久（秒）。宁可停久一点——用户明确说过"太连贯了，要强制停顿"。
_PAUSE_SECONDS = {
    "，": 0.45, ",": 0.45,
    "、": 0.38,
    "；": 0.55, ";": 0.55,
    "：": 0.50, ":": 0.50,
    "。": 1.00, ".": 1.00,
    "！": 1.00, "!": 1.00,
    "？": 0.90, "?": 0.90,
    "…": 0.75, "~": 0.30, "～": 0.30,
}
_MAX_SEGMENT = 12   # 一段超过这么多字就找地方断开，别让它连着念

# 这些字后面断一下最自然（语气词、"了/的"后面本来就会换气）
_SOFT_BREAK = "呀啦哦呢吧嘛啊欸诶嗯了着过"
_SOFT_PAUSE = 0.45


def _soft_split(segment: str, pause: float, limit: int = 0):
    """一段太长又没有标点，就在语气词或十来字处断一下。

    窗口要比 _MAX_SEGMENT 宽一点再往回找：「早呀爸爸今天有没有好好吃饭呢…」
    的语气词「呢」在 12 字开外，只取 12 字窗口就会从「好好吃/饭」中间硬切开。
    """
    pieces = []
    rest = segment
    while len(rest) > _MAX_SEGMENT:
        window = rest[: _MAX_SEGMENT + 6]
        cut = -1
        for index in range(len(window) - 1, 5, -1):
            if window[index] in _SOFT_BREAK:
                cut = index + 1
                break
        if cut < 0:
            cut = _MAX_SEGMENT
        pieces.append((rest[:cut].strip(), _SOFT_PAUSE))
        rest = rest[cut:].strip()
    if rest:
        pieces.append((rest, pause))
    return pieces


def _split_for_tts(text: str):
    """按标点切成 [(要合成的文本, 停顿时长)]。

    两个关键决定（都是用户反复试听后定的）：

    1. **该断就断**：不在逗号处断开，整句连成一片，听起来像机器人。
       所以照旧在每个标点处分段，并在段间插入停顿。
    2. **结尾送给模型的是「…」而不是逗号**：只送「爸爸」，模型说完就停，
       第二个"爸"听起来被切断；送「爸爸…」，模型会自己生成收尾语调，
       末字长 23%，末端响度从 0.076 降到 0.018（实测），听感自然得多。
       句末的 。！？ 保留原样。
    """
    pieces = []
    buffer = ""
    for char in text:
        if char in _PAUSE_SECONDS:
            body = buffer.strip()
            if body:
                if char in "。！？!?":
                    spoken = body + char
                else:
                    # 逗号 / 顿号 / 冒号 / 分号 → 用省略号收尾
                    spoken = body + "…"
                pieces.append([spoken, _PAUSE_SECONDS[char]])
            buffer = ""
        else:
            buffer += char

    if buffer.strip():
        # 结尾没标点，也给个省略号，避免急停
        pieces.append([buffer.strip() + "…", 0.30])

    # 一段太长还是念不完，再切（切出来的段没有省略号，属于罕见情况）
    result = []
    for segment, pause in pieces:
        if len(segment) > 18:
            result.extend(_soft_split(segment, pause))
        else:
            result.append([segment, pause])
    return [item for item in result if item[0]]

def _wsola(x, rate, factor, frame_ms: float = 42.0):
    """把一段音频拉长且不变调、无回声（WSOLA）。

    做法：按输出位置一格格铺窗，但每格的取样点在输入里**附近搜索**，
    选和上一格尾巴最相似的位置。重叠区相位对得上，就不会像复制那样
    产生梳状滤波（回声），也不会像变速那样变调。
    """
    if factor <= 1.001 or x.size < rate * 0.03:
        return x

    frame = int(rate * frame_ms / 1000.0)
    frame += frame % 2
    if frame < 64 or x.size < frame * 2:
        return x

    hop_out = frame // 2
    hop_in = max(1, int(round(hop_out / factor)))
    # 搜索范围必须**小于**步进，否则会往回跳、反复取同一小段——
    # 听感就是卡顿和"一个音重复"（2026-10 用户实测反馈）
    search = min(hop_out // 2, max(1, hop_in // 2 - 1), int(rate * 0.006))

    window = np.hanning(frame).astype("float32")
    out_len = int(x.size * factor) + frame
    out = np.zeros(out_len, dtype="float32")
    weight = np.zeros(out_len, dtype="float32")

    pos_in = 0
    pos_out = 0
    prev_tail = None

    while pos_in + frame + search < x.size and pos_out + frame < out_len:
        best = pos_in
        if prev_tail is not None:
            lo = max(0, pos_in - search)
            hi = min(x.size - frame, pos_in + search)
            if hi > lo:
                windows = np.lib.stride_tricks.sliding_window_view(
                    x[lo : hi + frame], frame
                )
                # 只比每个候选的**开头**那半格，和上一格的尾巴对齐
                corr = windows[:, :hop_out] @ prev_tail
                best = lo + int(np.argmax(corr))

        grain = x[best : best + frame]
        out[pos_out : pos_out + frame] += grain * window
        weight[pos_out : pos_out + frame] += window
        prev_tail = grain[hop_out:].copy()

        pos_in = best + hop_in
        pos_out += hop_out

    weight[weight < 1e-6] = 1.0
    out = out / weight
    valid = max(frame, out_len - frame)
    return out[:valid].astype("float32")


def _sustain_tail(
    samples: "np.ndarray",
    rate: int,
    tail_ms: float = 250.0,
    factor: float = 2.0,
    fade_ms: float = 200.0,
) -> "np.ndarray":
    """把这一段的结尾"拖"长一点。

    合成出来的每段都是"说完最后一个字就立刻停"，中间没有任何自然收音，
    听起来就是被切断的。这里取结尾一小段有声部分，用汉宁窗多次重叠叠加，
    相当于让最后那个音继续振动一会儿——音高不变，只变长。
    """
    if samples.size < rate * 0.05:
        return samples

    # 先去掉尾部静音，不然拖的是空气
    energy = np.abs(samples)
    voiced = np.where(energy > 0.004)[0]
    if voiced.size == 0:
        return samples
    end = int(voiced[-1]) + 1
    samples = samples[:end]

    if factor > 1.001:
        n = int(rate * tail_ms / 1000.0)
        n = min(n, samples.size // 2) if samples.size > 8 else 0
        if n >= int(rate * 0.02):
            stretched = _wsola(samples[-n:], rate, factor)
            samples = np.concatenate([samples[:-n], stretched])
    merged = samples

    # 关键：让它"衰减下去"，而不是在满音量处被切断。
    # 实测原样结尾还有 0.09~0.13 的响度（正常说话 0.15~0.23），听感就是一刀切。
    # 用指数曲线收尾，像真的人说完话气息散掉。
    fade_ms = max(0.0, float(fade_ms))
    fade_n = int(rate * fade_ms / 1000.0)
    fade_n = min(fade_n, merged.size)
    if fade_n > 16:
        curve = np.linspace(0.0, 1.0, fade_n, dtype="float32")
        curve = np.exp(-3.2 * curve)  # 前段缓、后段快，接近自然衰减
        merged[-fade_n:] *= curve
    elif fade_n > 0:
        merged[-fade_n:] *= np.linspace(1.0, 0.0, fade_n, dtype="float32")
    return merged


def synth_zipvoice(text: str) -> Path:
    """用米米的声音念一段话（零样本克隆，跑在服务器本地）。

    按标点分句、逐句合成，再在句间插静音——这样逗号句号处会有自然的停顿，
    而不会一口气念到底。
    """
    import wave

    engine, (ref_text, prompt, ref_rate) = _zipvoice_engine()

    # 太长的语音要合成一分钟以上，主人等不了。超长就截到最后一个标点。
    limit = config.ZIPVOICE_MAX_CHARS
    if limit and len(text) > limit:
        head = text[:limit]
        cut = max(head.rfind(mark) for mark in "，。！？；、,.!?;")
        text = head[: cut + 1] if cut > limit // 2 else head
        log.info("语音太长，截到 %d 字", len(text))

    segments = _split_for_tts(text)
    if not segments:
        raise TTSError("没有可念的内容")

    rate = engine.sample_rate
    scale = config.ZIPVOICE_PAUSE_SCALE
    chunks = []
    for index, (segment, pause) in enumerate(segments):
        audio = engine.generate(
            segment,
            ref_text,
            prompt,
            ref_rate,
            speed=config.ZIPVOICE_SPEED,
            num_steps=config.ZIPVOICE_STEPS,
        )
        samples = np.array(audio.samples, dtype="float32")
        if samples.size == 0:
            continue
        # 每段结尾都拖一下，最后一个字就不像被切掉了
        samples = _sustain_tail(
            samples,
            rate,
            tail_ms=config.ZIPVOICE_TAIL_MS,
            factor=config.ZIPVOICE_TAIL_FACTOR,
            fade_ms=config.ZIPVOICE_TAIL_FADE_MS,
        )
        chunks.append(samples)
        if index < len(segments) - 1 and pause > 0:
            # 每段头尾都削掉一点，避免合成自带的气音把停顿吃掉
            chunks.append(np.zeros(int(rate * pause * scale), dtype="float32"))

    if not chunks:
        raise TTSError("ZipVoice 没生成出音频")

    merged = np.concatenate(chunks)
    # 头尾各留 80ms 静音，QQ 那边听着不那么突兀
    pad = np.zeros(int(rate * 0.08), dtype="float32")
    merged = np.concatenate([pad, merged, pad])

    out = _new_path(".wav")
    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((np.clip(merged, -1, 1) * 32767).astype(np.int16).tobytes())
    log.info("语音分为 %d 段，共 %.1f 秒", len(segments), len(merged) / rate)
    return out


# ---------------------------------------------------------------- 入口

def _engine_func():
    engines = {
        "zipvoice": synth_zipvoice,
        "espeak": synth_espeak,
        "tencent": synth_tencent,
        "aliyun": synth_aliyun,
    }
    func = engines.get(config.TTS_ENGINE)
    if func is None:
        raise TTSError(f"不认识的 TTS_ENGINE：{config.TTS_ENGINE}")
    return func


async def synthesize(text: str) -> Path:
    """把文字合成成音频文件，返回路径。

    espeak 走子进程、腾讯云走网络，两者都是阻塞调用，
    所以丢进线程池，别把整个事件循环卡住。
    """
    raw = (text or "").strip()
    if not raw:
        raise TTSError("没有可合成的内容")

    cleaned = _sanitize(raw)
    if not cleaned:
        raise TTSError(f"这段内容里没有能念的字：{raw[:40]}")

    if len(cleaned) > config.TTS_MAX_CHARS:
        cleaned = cleaned[: config.TTS_MAX_CHARS]

    log.info("TTS 文本（%d 字）：%s", len(cleaned), cleaned[:80])

    func = _engine_func()
    path = await asyncio.to_thread(func, cleaned)
    _cleanup()
    return path
