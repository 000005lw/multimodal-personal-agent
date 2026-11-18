"""配置加载：只依赖标准库，从同目录的 .env 读取。"""

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


def _load_env(path: Path) -> None:
    """极简 .env 解析器，省掉 python-dotenv 依赖。

    注意：值里不要写行内注释（`X=1  # 注释` 会被当成值的一部分）。
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # 已存在的真实环境变量优先，方便临时覆盖
        os.environ.setdefault(key, value)


_load_env(BASE_DIR / ".env")


def _get(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_get(name) or default)
    except ValueError:
        return default


# ---------- 模型 ----------
API_KEY = _get("DEEPSEEK_API_KEY")
BASE_URL = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = _get("DEEPSEEK_MODEL", "deepseek-chat")
TEMPERATURE = _float("DEEPSEEK_TEMPERATURE", 1.3)
MAX_TOKENS = _int("DEEPSEEK_MAX_TOKENS", 800)
# 离线自测模式：不调真模型，直接回显
MOCK_LLM = _flag("MOCK_LLM")

# ---------- 谁能和它聊 ----------
_owner_raw = _get("OWNER_QQ")
OWNER_QQS = {int(x) for x in re.split(r"[,;\s]+", _owner_raw) if x.isdigit()}

# ---------- 朋友（可以聊天，但不是主人） ----------
# 逗号分开的 QQ 号。他们能聊天、发语音、点歌、画画、搜图，
# 但拿不到主人的东西：不说"爸爸"、不主动找你、不发说说、不参与看视频申请。
_friend_raw = _get("FRIEND_QQ")
FRIEND_QQS = {int(x) for x in re.split(r"[,;\s]+", _friend_raw) if x.isdigit()}
# 谁能触发回应（主人 + 朋友），其他人依然静默丢弃
ALLOWED_QQS = OWNER_QQS | FRIEND_QQS

# 朋友那边的称呼：不叫爸爸，用"你"
FRIEND_NO_DADDY = _flag("FRIEND_NO_DADDY", True)
# 频率限制：每人每天最多点几首歌
FRIEND_SONGS_PER_DAY = _int("FRIEND_SONGS_PER_DAY", 3)
# 每人每分钟最多几句话（超了就冷处理一下）
FRIEND_MSGS_PER_MIN = _int("FRIEND_MSGS_PER_MIN", 8)
REPLY_TO_GROUPS = _flag("REPLY_TO_GROUPS", False)
GROUP_WHITELIST = {
    int(x) for x in re.split(r"[,;\s]+", _get("GROUP_WHITELIST")) if x.isdigit()
}

# ---------- 机器人自己监听 ----------
HOST = _get("BOT_HOST", "127.0.0.1")
PORT = _int("BOT_PORT", 8090)

# ---------- OneBot / NapCat ----------
ONEBOT_API_BASE = _get("ONEBOT_API_BASE", "http://127.0.0.1:3000").rstrip("/")
ONEBOT_TOKEN = _get("ONEBOT_ACCESS_TOKEN")

# ---------- 行为 ----------
QUOTE_REPLY = _flag("QUOTE_REPLY", True)
HISTORY_TURNS = _int("HISTORY_TURNS", 120)
# 历史逐字保留的上限（字），超了就触发压缩
MAX_HISTORY_CHARS = _int("MAX_HISTORY_CHARS", 40000)
# 压缩出来的长期摘要，字数上限
SUMMARY_MAX_CHARS = _int("SUMMARY_MAX_CHARS", 1500)
MAX_REPLY_CHARS = _int("MAX_REPLY_CHARS", 900)
SIMULATE_TYPING = _flag("SIMULATE_TYPING", True)

# 轻量工具运行时。默认关闭，先通过 selfcheck/回放验证再逐步启用。
ACTIONS_V2 = _flag("ACTIONS_V2", False)
ACTION_TRACE_ENABLED = _flag("ACTION_TRACE_ENABLED", True)
ACTION_TRACE_FILE = _get("ACTION_TRACE_FILE", "action_trace.jsonl")

# 收到消息后先"想一下"多久才开口（秒）。调大就更像真人，不是秒回。
REPLY_PAUSE_MIN = _float("REPLY_PAUSE_MIN", 2.0)
REPLY_PAUSE_MAX = _float("REPLY_PAUSE_MAX", 4.5)
# 每条气泡之间再等多久（秒），在按字数算的时间上再叠加
BUBBLE_GAP_MIN = _float("BUBBLE_GAP_MIN", 0.8)
BUBBLE_GAP_MAX = _float("BUBBLE_GAP_MAX", 2.0)
# 打字速度：每秒几个字。真人手打大约 4~8，越小回复越慢
TYPING_CHARS_PER_SEC = _float("TYPING_CHARS_PER_SEC", 6.0)
# 每条气泡最少/最多等多久（含打字时间，不含 BUBBLE_GAP 的随机量）
TYPING_BUBBLE_MIN = _float("TYPING_BUBBLE_MIN", 1.5)
TYPING_BUBBLE_MAX = _float("TYPING_BUBBLE_MAX", 6.0)
# 对方一口气发好几句时，先等这么久再回；期间来了新消息就重新计时
MERGE_ENABLED = _flag("MERGE_ENABLED", True)
MERGE_WAIT_SECONDS = _float("MERGE_WAIT_SECONDS", 6.0)
# 最多攒这么久就必须回了，免得对方一直发、她一直等
MERGE_MAX_SECONDS = _float("MERGE_MAX_SECONDS", 25.0)
# 主动找朋友聊天：频率和找主人一样（20~60 分钟一次），但有几道保险
FRIEND_PROACTIVE_ENABLED = _flag("FRIEND_PROACTIVE_ENABLED", True)
# 对方多久没说话，才算"该找他了"
FRIEND_PROACTIVE_IDLE_MINUTES = _int("FRIEND_PROACTIVE_IDLE_MINUTES", 120)
FRIEND_PROACTIVE_MIN_MINUTES = _int("FRIEND_PROACTIVE_MIN_MINUTES", 180)
FRIEND_PROACTIVE_MAX_MINUTES = _int("FRIEND_PROACTIVE_MAX_MINUTES", 360)
# 每个朋友一天最多被主动找几次（防刷、防风控）
FRIEND_PROACTIVE_MAX_PER_DAY = _int("FRIEND_PROACTIVE_MAX_PER_DAY", 5)
# 对方连续多少天没回过话，就不再主动找他（用户 2026-11-09 要求：5 天）
# 一开口就自动恢复，不用手动解封
FRIEND_SILENT_STOP_DAYS = _int("FRIEND_SILENT_STOP_DAYS", 5)
# 凌晨也算能开口，但内容必须对得上时间（用户要求）
PROACTIVE_ALLOW_NIGHT = _flag("PROACTIVE_ALLOW_NIGHT", True)
# 把回复拆成 1~3 条短消息发出去，更像真人打字
MULTI_BUBBLE = _flag("MULTI_BUBBLE", True)
# 一次最多发几条消息，超过就合并尾巴
MULTI_BUBBLE_MAX = _int("MULTI_BUBBLE_MAX", 5)
# 硬过滤括号里的动作描写（模型会模仿自己历史里的旧习惯）
STRIP_ACTIONS = _flag("STRIP_ACTIONS", True)

# ---------- 语音 ----------
# 总开关
TTS_ENABLED = _flag("TTS_ENABLED", True)
# 引擎：zipvoice（绘梨衣音色，服务器本地跑） / aliyun / tencent / espeak
TTS_ENGINE = _get("TTS_ENGINE", "espeak")

# ---------- ZipVoice（绘梨衣音色，零样本克隆） ----------
ZIPVOICE_DIR = Path(_get("ZIPVOICE_DIR", "/home/ubuntu/whale-tts/models-zipvoice"))
ZIPVOICE_VOICE_DIR = Path(_get("ZIPVOICE_VOICE_DIR", "/home/ubuntu/whale-tts/voices/newvoice"))
# 推理步数：4 快（RTF≈2）、8 慢一倍但更细腻
ZIPVOICE_STEPS = _int("ZIPVOICE_STEPS", 4)
ZIPVOICE_SPEED = _float("ZIPVOICE_SPEED", 1.0)
ZIPVOICE_THREADS = _int("ZIPVOICE_THREADS", 2)
# 语音最多念多少字。实测约 0.9 秒/字，40 字要 35 秒左右，再多主人就等太久了
ZIPVOICE_MAX_CHARS = _int("ZIPVOICE_MAX_CHARS", 40)
# 停顿倍率：嫌不够就调大（1.5 表示所有停顿都变 1.5 倍）
ZIPVOICE_PAUSE_SCALE = _float("ZIPVOICE_PAUSE_SCALE", 1.0)
# 段尾拖音：最后那个字合成完会立刻断，听起来像被切掉。
# TAIL_MS 是被拖的那一小段长度，TAIL_FACTOR 是拉长倍数（1.0 = 不处理）。
ZIPVOICE_TAIL_MS = _float("ZIPVOICE_TAIL_MS", 0.0)
ZIPVOICE_TAIL_FACTOR = _float("ZIPVOICE_TAIL_FACTOR", 1.0)
# 段尾衰减长度：越长越像自然收音，太短就又变成"切"
ZIPVOICE_TAIL_FADE_MS = _float("ZIPVOICE_TAIL_FADE_MS", 40.0)
# 一句话超过这么多字，才在逗号处补一刀；短句整句合成，避免逗号前那个字被切断
ZIPVOICE_COMMA_SPLIT_CHARS = _int("ZIPVOICE_COMMA_SPLIT_CHARS", 26)
# espeak 的中文音色是 cmn；语速和音调稍微调过，别太像机器念稿
TTS_VOICE = _get("TTS_VOICE", "cmn")
TTS_SPEED = _int("TTS_SPEED", 165)
TTS_PITCH = _int("TTS_PITCH", 70)
# 一段语音最多合成多少字，太长文件会很大
TTS_MAX_CHARS = _int("TTS_MAX_CHARS", 200)
# 发送方式：base64 最省事，不受路径和权限影响
TTS_SEND_MODE = _get("TTS_SEND_MODE", "base64")

# ---------- 腾讯云语音合成 ----------
TENCENT_SECRET_ID = _get("TENCENT_SECRET_ID")
TENCENT_SECRET_KEY = _get("TENCENT_SECRET_KEY")
TENCENT_TTS_REGION = _get("TENCENT_TTS_REGION", "ap-guangzhou")
# 音色 ID，101001 是常见的女声，具体可以在控制台试听里换
TENCENT_TTS_VOICE = _int("TENCENT_TTS_VOICE", 101001)
# 语速 -2 ~ 2，0 是正常
TENCENT_TTS_SPEED = _float("TENCENT_TTS_SPEED", 0.0)
# 返回的音频格式，mp3 最通用
TENCENT_TTS_CODEC = _get("TENCENT_TTS_CODEC", "mp3")

# ---------- 阿里云语音合成 ----------
ALIYUN_ACCESS_KEY_ID = _get("ALIYUN_ACCESS_KEY_ID")
ALIYUN_ACCESS_KEY_SECRET = _get("ALIYUN_ACCESS_KEY_SECRET")
# 智能语音交互控制台创建项目后拿到的 AppKey
ALIYUN_APPKEY = _get("ALIYUN_APPKEY")
ALIYUN_NLS_REGION = _get("ALIYUN_NLS_REGION", "cn-shanghai")
# 音色，xiaoyun 是默认女声
ALIYUN_TTS_VOICE = _get("ALIYUN_TTS_VOICE", "xiaoyun")
# 语速、音调，范围 -500 ~ 500，0 正常
ALIYUN_TTS_SPEECH_RATE = _int("ALIYUN_TTS_SPEECH_RATE", 0)
ALIYUN_TTS_PITCH_RATE = _int("ALIYUN_TTS_PITCH_RATE", 0)

PERSONA_FILE = (BASE_DIR / _get("PERSONA_FILE", "persona.md")).resolve()
MEMORY_FILE = DATA_DIR / "memory.json"
RUNTIME_DB_FILE = DATA_DIR / _get("RUNTIME_DB_FILE", "runtime.db")
JOB_DB_FILE = DATA_DIR / _get("JOB_DB_FILE", "jobs.db")
JOB_LEASE_SECONDS = _int("JOB_LEASE_SECONDS", 1800)
JOB_MAX_ATTEMPTS = _int("JOB_MAX_ATTEMPTS", 3)
JOB_RETENTION_DAYS = _int("JOB_RETENTION_DAYS", 7)
JOB_RESULT_MAX_BYTES = _int("JOB_RESULT_MAX_BYTES", 16 * 1024 * 1024)
LOG_LEVEL = _get("LOG_LEVEL", "INFO").upper()

# ---------- 图片理解 ----------
# 总开关
VISION_ENABLED = _flag("VISION_ENABLED", True)
# 带图片的请求要用支持视觉的模型。deepseek-flash 支持，deepseek-v4-pro 不支持。
VISION_MODEL = _get("DEEPSEEK_VISION_MODEL", "deepseek-flash")
# 一条消息最多看几张图
VISION_MAX_IMAGES = _int("VISION_MAX_IMAGES", 3)
# 单张图最大字节数，超过就跳过（避免把内存和请求体撑爆）
VISION_MAX_BYTES = _int("VISION_MAX_BYTES", 8 * 1024 * 1024)

# ---------- 生图（通义万相） ----------
IMAGE_ENABLED = _flag("IMAGE_ENABLED", True)
# 百炼控制台的 API-KEY，跟语音用的 AccessKey 不是一回事
DASHSCOPE_API_KEY = _get("DASHSCOPE_API_KEY")
# 模型：wanx2.1-t2i-turbo 快而便宜，wanx2.1-t2i-plus 更精细
IMAGE_MODEL = _get("IMAGE_MODEL", "wanx2.0-t2i-turbo")
IMAGE_SIZE = _get("IMAGE_SIZE", "1024*1024")
# 最多等多少秒
IMAGE_TIMEOUT = _int("IMAGE_TIMEOUT", 90)

# 她自己的长相：画「她自己」时强制套用，否则每次都是另一个蓝发少女
IMAGE_SELF_DESC = _get(
    "IMAGE_SELF_DESC",
    "Q版三头身动漫少女，深蓝色及腰长直发，头顶一根翘起的呆毛，大而圆的蓝色眼睛，"
    "白色蕾丝女仆头饰缀蓝色蝴蝶结，头侧有淡蓝色鲸鱼鳍状发饰，"
    "藏青色侍女连衣裙外罩白色蕾丝围裙，围裙正中印着一只蓝色小鲸鱼，"
    "身后拖着一条蓝色鲸鱼尾巴，双手捧在胸前，笑得眼睛弯起来",
)
IMAGE_SELF_STYLE = _get(
    "IMAGE_SELF_STYLE",
    "日系动漫插画，Q版，粗描边，干净线条，柔和光影，奶油色简洁背景",
)
# 提示词里出现这些词，就认定是在画她自己
IMAGE_SELF_KEYS = ("自己", "鲸鲸", "画我", "小鲸鱼")

# 参考图：放在 data/ 下，画「她自己」时以它为准，这样脸才不会每次变
IMAGE_SELF_REF = _get("IMAGE_SELF_REF", "ref_whale.png")
# 图生图用的模型和功能
#   description_edit 会真的按描述改动作和场景（推荐）
#   stylization_all  只换画风，姿势几乎不变
IMAGE_EDIT_MODEL = _get("IMAGE_EDIT_MODEL", "wanx2.1-imageedit")
IMAGE_EDIT_FUNCTION = _get("IMAGE_EDIT_FUNCTION", "description_edit")

# ---------- 主动发消息 ----------
# 隔一阵子自己找个话题来找你说话
PROACTIVE_ENABLED = _flag("PROACTIVE_ENABLED", True)
# 两次之间的随机间隔（分钟）
PROACTIVE_MIN_MINUTES = _int("PROACTIVE_MIN_MINUTES", 90)
PROACTIVE_MAX_MINUTES = _int("PROACTIVE_MAX_MINUTES", 180)
# 最近这么多分钟内聊过，就不主动打扰
PROACTIVE_IDLE_MINUTES = _int("PROACTIVE_IDLE_MINUTES", 120)
# 免打扰时段（小时，含头不含尾；起点比终点大表示跨零点）
PROACTIVE_QUIET_START = _int("PROACTIVE_QUIET_START", 1)
PROACTIVE_QUIET_END = _int("PROACTIVE_QUIET_END", 8)

# ---------- 没回就追一句 ----------
# 她主动说完、你半天不理，隔几分钟她会接着话题再补一句
FOLLOWUP_ENABLED = _flag("FOLLOWUP_ENABLED", True)
FOLLOWUP_MIN_MINUTES = _int("FOLLOWUP_MIN_MINUTES", 20)
FOLLOWUP_MAX_MINUTES = _int("FOLLOWUP_MAX_MINUTES", 40)
# 最多连补几句，补完就安静等你
FOLLOWUP_MAX_TIMES = _int("FOLLOWUP_MAX_TIMES", 2)
# 她正常回完一句话之后你没接话，要不要也追一句
FOLLOWUP_AFTER_REPLY = _flag("FOLLOWUP_AFTER_REPLY", True)
# 正常回复后追话的间隔（分钟），比主动开口那次久一些，免得像催命
FOLLOWUP_REPLY_MIN = _int("FOLLOWUP_REPLY_MIN", 60)
FOLLOWUP_REPLY_MAX = _int("FOLLOWUP_REPLY_MAX", 120)

# ---------- 健康巡检 ----------
# 每隔多少秒摸一次 NapCat，掉线和恢复都打日志。0 = 关闭
WATCHDOG_SECONDS = _int("WATCHDOG_SECONDS", 300)
# 掉线后先只通知、不重启。只有 NapCat 进程真的没了、且持续这么久，才重启一次
WATCHDOG_RESTART_AFTER_MINUTES = _int("WATCHDOG_RESTART_AFTER_MINUTES", 10)
# 两次自动重启之间至少隔这么久（防止反复重启把快速登录打断）
WATCHDOG_RESTART_COOLDOWN_MINUTES = _int("WATCHDOG_RESTART_COOLDOWN_MINUTES", 30)
# 现在用的是哪个后端：true = NapCat（看门狗可以重启它），false = 别的（比如 SnowLuma 容器）
NAPCAT_MANAGED = _flag("NAPCAT_MANAGED", False)

# ---------- 掉线邮件提醒 ----------
MAIL_ENABLED = _flag("MAIL_ENABLED", True)
MAIL_SMTP_HOST = _get("MAIL_SMTP_HOST", "smtp.qq.com")
MAIL_SMTP_PORT = _int("MAIL_SMTP_PORT", 465)
MAIL_FROM = _get("MAIL_FROM")
# QQ 邮箱的授权码，不是登录密码
MAIL_AUTH_CODE = _get("MAIL_AUTH_CODE")
MAIL_TO = _get("MAIL_TO")
# 收信（IMAP）：主人发邮件要二维码时用
MAIL_IMAP_ENABLED = _flag('MAIL_IMAP_ENABLED', True)
MAIL_IMAP_HOST = _get('MAIL_IMAP_HOST', 'imap.qq.com')
MAIL_IMAP_PORT = _int('MAIL_IMAP_PORT', 993)

# ---------- 表情包 ----------
# 图放在 data/stickers/<情绪>/*.jpg，模型写 [[贴图:情绪]] 就会发一张
# 隔多少分钟没聊，就当她重新开口（不再接着上一个话题）
TOPIC_RESET_MIN = 45

STICKERS_ENABLED = _flag("STICKERS_ENABLED", True)# 一条回复里最多发几个表情包，防止刷屏
STICKER_MAX_PER_REPLY = _int("STICKER_MAX_PER_REPLY", 1)
# 模型没主动发表情包时，按这个概率随机补一张（0~1）
STICKER_PROBABILITY = _float("STICKER_PROBABILITY", 0.5)

# ---------- 链接解析 ----------
# 主人发 B站 / 抖音分享链接时，读一下标题和作者，让她知道在看什么
LINK_PARSE_ENABLED = _flag("LINK_PARSE_ENABLED", True)

# ---------- 联网搜索 ----------
# 她可以写 [[搜索:关键词]]，系统替她上网查，然后把结果塞回去再生成
SEARCH_ENABLED = _flag("SEARCH_ENABLED", True)
SEARCH_COUNT = _int("SEARCH_COUNT", 5)
# 搜索时顺便把正文抓下来，她回答得更深（费一点流量，但内容更实）
SEARCH_WITH_CONTENT = _flag("SEARCH_WITH_CONTENT", True)
# 每条搜索结果正文最多取多少字
SEARCH_CONTENT_CHARS = _int("SEARCH_CONTENT_CHARS", 700)

# ---------- 读网页 ----------
# 主人发任意链接，她都能打开读内容（B站抖音另走解析）
READING_ENABLED = _flag("READING_ENABLED", True)
# 一条消息里最多读几个链接
READING_MAX_PAGES = _int("READING_MAX_PAGES", 2)
# 每个页面最多塞多少字进上下文
READING_MAX_CHARS = _int("READING_MAX_CHARS", 2500)

# ---------- 家里电脑中转 ----------
# B站抖音封机房 IP，让家里的电脑去读，读不到再退回直连
HOME_BRIDGE_ENABLED = _flag("HOME_BRIDGE_ENABLED", True)
HOME_TOKEN = _get("HOME_TOKEN", "")
# 等家里电脑回话最多几秒
HOME_TIMEOUT = _int("HOME_TIMEOUT", 25)

# ---------- 搜图发图 ----------
IMAGE_SEARCH_ENABLED = _flag("IMAGE_SEARCH_ENABLED", True)
# 发出去之前让视觉模型看一眼，对不对得上关键词
IMAGE_SEARCH_VERIFY = _flag("IMAGE_SEARCH_VERIFY", True)
# 一次最多发几张
IMAGE_SEARCH_MAX = _int("IMAGE_SEARCH_MAX", 3)
# 小于这个字节数的当图标丢掉
IMAGE_SEARCH_MIN_BYTES = _int("IMAGE_SEARCH_MIN_BYTES", 20000)

# ---------- 视频抽帧 ----------
# 需要服务器装 yt-dlp 和 ffmpeg，没装会自动跳过
VIDEO_FRAMES_ENABLED = _flag("VIDEO_FRAMES_ENABLED", False)
VIDEO_MAX_FRAMES = _int("VIDEO_MAX_FRAMES", 4)
# 旁白最多转写多少分钟（阿里云语音识别按分钟计费）
VIDEO_ASR_MINUTES = _int("VIDEO_ASR_MINUTES", 30)

# ---------- 每天自己去看视频 ----------
# 到点先发消息问主人同不同意，同意了她才去 B站挑一个看
WATCH_VIDEO_ENABLED = _flag("WATCH_VIDEO_ENABLED", True)
# 每天随机问几次（0 表示退回固定时间 WATCH_VIDEO_HOUR）
WATCH_VIDEO_TIMES_PER_DAY = _int("WATCH_VIDEO_TIMES_PER_DAY", 4)
# 在哪个时间段里随机挑（24 小时制，含头不含尾）
WATCH_VIDEO_FROM_HOUR = _int("WATCH_VIDEO_FROM_HOUR", 9)
WATCH_VIDEO_TO_HOUR = _int("WATCH_VIDEO_TO_HOUR", 23)
# 固定时间（只在 TIMES_PER_DAY<=0 时用）
WATCH_VIDEO_HOUR = _int("WATCH_VIDEO_HOUR", 16)

# ---------- 唱歌 ----------
# 主人说"唱一首 X"，她自己去B站找歌、分离人声、换成绘梨衣唱回来
SING_ENABLED = _flag("SING_ENABLED", True)
# 截多长（秒）。QQ 语音最长约 60 秒，别设太大
SING_SECONDS = _int("SING_SECONDS", 60)
# 从第几秒开始截（跳过前奏）
SING_SKIP_SECONDS = _int("SING_SKIP_SECONDS", 15)
# 整条要几分钟：下载+分离+转换，得留够时间
SING_TIMEOUT = _int("SING_TIMEOUT", 900)
# 中继（家里那台）至少要这个代码版本；旧版做不出"整首分段"
HOME_MIN_VERSION = _int("HOME_MIN_VERSION", 7)   # 2026-10-29 选歌修复，旧中继不认

# ---------- 唱歌音质 ----------
# 变调目标音高。绘梨衣参考音是 262Hz，但追太高会把原曲高音顶出模型音域 → 沙哑。
# 压到 235Hz 听着还是女声，高音不容易破。
SING_TARGET_F0 = _float("SING_TARGET_F0", 235.0)
# 最多升几个半音。12 是整八度，高音很容易挤沙，默认收到 9。
SING_MAX_SHIFT = _int("SING_MAX_SHIFT", 7)
# protect：越大越保护气声和高音（不沙哑），越小咬字越清楚
SING_PROTECT = _float("SING_PROTECT", 0.6)

# ---------- 性格演化 ----------
# 每隔多少轮对话，让她重写一次"我最近的变化"
EVOLVE_ENABLED = _flag("EVOLVE_ENABLED", True)
EVOLVE_EVERY_TURNS = _int("EVOLVE_EVERY_TURNS", 25)
EVOLVE_MAX_CHARS = _int("EVOLVE_MAX_CHARS", 400)

# ---------- QQ 空间说说 ----------
# 每天自己发几条说说（前提：这个号必须开通过 QQ 空间）
QZONE_ENABLED = _flag("QZONE_ENABLED", True)
# 每天发几条：每天在这个区间里随机取一个数
QZONE_TIMES_MIN = _int("QZONE_TIMES_MIN", 5)
QZONE_TIMES_MAX = _int("QZONE_TIMES_MAX", 15)
QZONE_FROM_HOUR = _int("QZONE_FROM_HOUR", 9)
QZONE_TO_HOUR = _int("QZONE_TO_HOUR", 23)

# B站登录态 Cookie（SESSDATA）。填了就能从机房 IP 访问 B站，不必借家里电脑
BILI_COOKIE = _get('BILI_COOKIE', '')
# 出网代理。填了之后 B站/抖音 的请求走它（住宅 IP 能过 412）
PROXY_URL = _get('PROXY_URL', '')
# NapCat 生成的登录二维码路径
QRCODE_PATH = Path("/opt/QQ/resources/app/napcat/cache/qrcode.png")
# 什么时候把抽出来的帧发回去：
#   ask    —— 只有主人明确要（默认）
#   always —— 每次都发
#   never  —— 从不发，只让她自己看
VIDEO_SEND_MODE = _get("VIDEO_SEND_MODE", "ask").lower()


def load_persona() -> str:
    """人设文本，直接读 md 文件，改完重启即生效。"""
    if PERSONA_FILE.exists():
        return PERSONA_FILE.read_text(encoding="utf-8").strip()
    return "你是「小鲸鱼」，一只住在服务器里的鲸鱼。说话简短、口语化，不要客服腔。"


def check() -> list[str]:
    """启动前自检，返回需要用户处理的问题列表。"""
    problems: list[str] = []
    if not MOCK_LLM and not API_KEY:
        problems.append(
            "缺少 DEEPSEEK_API_KEY（到 https://platform.deepseek.com/api_keys 申请，填进 .env）"
        )
    if not OWNER_QQS:
        problems.append("缺少 OWNER_QQ（填你自己的 QQ 号，否则它不会回复任何人）")
    if not 1 <= PORT <= 65535:
        problems.append(f"BOT_PORT 不合法：{PORT}")
    if not PERSONA_FILE.exists():
        problems.append(f"找不到人设文件：{PERSONA_FILE}")
    return problems
