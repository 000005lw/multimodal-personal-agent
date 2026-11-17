"""小鲸鱼 QQ 机器人主程序。

链路：
  QQ → NapCat →（HTTP 上报）→ 本程序 → DeepSeek API
                                  ↓
                             NapCat HTTP API → QQ

只回复 .env 里 OWNER_QQ 指定的号。别人发消息直接静默丢弃。
"""

import asyncio
import base64
import json
import logging
import random
import re
import subprocess
import sys
import time
import uuid
from collections import deque

from aiohttp import web

import config
import llm
import evolve
import emotion
import imagegen
import media
import memory
import notify
import onebot
import sing
import songlib
import voice
import stickers
import sysinfo
import neuron
import affection
import mood
import rules
import remind
import voice

log = logging.getLogger("whale")

# 家里电脑中转：任务队列 + 等着收结果的 future
JOBS: asyncio.Queue | None = None
PENDING: dict = {}
# 家里人那台中继上次报上来的代码版本：{"value": 4, "ts": ...}
HOME_VERSION: dict = {"value": 0, "ts": 0.0}

# Windows 控制台编码不是 UTF-8 时，含特殊字符的日志会直接抛 UnicodeEncodeError。
# 这里只把错误策略改成 replace，不动编码本身（否则中文会变乱码）。
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    except Exception:
        pass

# 把 markdown 残留擦掉：QQ 不渲染，留着只会变成一堆符号
_CLEANERS = [
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"```[a-zA-Z0-9_+\-]*\n?"), ""),
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"),
    (re.compile(r"__(.+?)__", re.DOTALL), r"\1"),
    (re.compile(r"~~(.+?)~~", re.DOTALL), r"\1"),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\n{2,}"), "\n"),  # 连着说，别隔行
]

_SENTENCE_END = "。！？!?…~.\n；;"

# 模型用这个标记请求发表情包，写成 [[贴图:开心]] 或 [[表情:无语]]
_STICKER_RE = re.compile(r"\[\[(?:贴图|表情)[:：]\s*([^\[\]]{1,12})\]\]")
# 模型用这个标记请求上网查，写成 [[搜索:关键词]]
_SEARCH_RE = re.compile(r"\[\[搜索[:：]\s*([^\[\]]{1,40})\]\]")
# 模型用这个标记表示"主人想看图，把截帧发给他"
_SHOT_RE = re.compile(r"\[\[截图\]\]")
# 模型用这个标记请求画图，写成 [[画:一只在吃米饭的小鲸鱼]]
_DRAW_RE = re.compile(r"\[\[画[:：]\s*([^\[\]]{1,120})\]\]")
# 模型用这个标记请求网上找图，写成 [[找图:蓝色小鲸鱼壁纸]]
_FINDIMG_RE = re.compile(r"\[\[找图[:：]\s*([^\[\]]{1,40})\]\]")
# 模型用这个标记请求唱歌，写成 [[唱:起风了]]
_SING_RE = re.compile(r"\[\[唱[:：]\s*([^\[\]]{1,40})\]\]")
# 主人话里出现这些，就认为是要她唱一首
_SING_HINTS = (
    "唱一首", "唱个", "唱一下", "来一首", "给我唱", "你唱", "唱首", "来首", "会唱",
    # 用户口语里更常见的是「想听」——少了这几条，千鹤说「我想听《雨爱》」
    # 就抽不出歌名，模型会自己编个搜索词（实测编成了「歌」，搜到个搞笑视频）
    "想听", "我要听", "想听听", "点一首", "点个", "放一首", "放一下",
)
# 主人话里出现这些，就直接去网上找图
_FINDIMG_HINTS = ("搜图", "找图", "搜几张", "找几张", "来点图", "找一些图", "搜一下图")

# 主人话里出现这些，就认为是想要截图
_FRAME_HINTS = (
    "截图", "发图", "发张图", "发几张", "图发我", "图给我",
    "把图", "画面发", "截个图", "给我看",
)


def _qq_of(cid: str) -> int:
    """从会话 id（u1000000001 / g123）里取出 QQ 号。"""
    digits = "".join(ch for ch in str(cid) if ch.isdigit())
    return int(digits) if digits else 0


def is_owner(cid: str) -> bool:
    return _qq_of(cid) in config.OWNER_QQS


def is_friend(cid: str) -> bool:
    return _qq_of(cid) in config.FRIEND_QQS


def wants_frames(text: str) -> bool:
    candidate = (text or "").strip()
    return any(hint in candidate for hint in _FRAME_HINTS)


def wants_sing(text: str) -> bool:
    """主人是不是想让她唱一首。"""
    candidate = (text or "").strip()
    if not candidate or "唱歌" in candidate and len(candidate) <= 4:
        return False
    return any(hint in candidate for hint in _SING_HINTS)


# 这些不是歌名，抠出来也不能拿去搜
_GENERIC_SONG = {"歌", "歌曲", "音乐", "那首歌", "这首歌", "歌名", "曲子", "个", "首"}


def pick_song_name(text: str) -> str:
    """从"唱一首 起风了"里把歌名抠出来。"""
    # 先把书名号/引号里的内容当成歌名候选（「我想听《雨爱》」）
    wrapped = re.search(r"[《「\"']([^》」\"']{1,20})[》」\"']", text or "")
    candidate = re.sub(r"[，。！？!?,.\s]+$", "", (text or "").strip())
    for hint in _SING_HINTS:
        if hint in candidate:
            rest = candidate.split(hint, 1)[1]
            rest = re.sub(
                r"^(一下|一首|一个|首歌|首|吧|呗|好吗|好不好|听听|来)", "", rest.strip()
            )
            # 「我要听你唱宝船」→ 剥掉主语和动词，剩「宝船」
            # （不剥的话会拿「你唱宝船」去搜，搜不到就乱放上一首）
            rest = re.sub(r"^(你|我|他|她|人家|给我|帮我|爸爸|爸爸给我|爸爸给你)+", "", rest.strip())
            rest = re.sub(r"^(唱|哼|来|放)(一首|一个|首|一下|遍)?", "", rest.strip())
            # 尾巴上的客套话要去干净：「红豆给我吧」「晴天听一下」都要能剩歌名
            rest = re.sub(
                r"(给我|给爸爸|来|听|唱|吧|呗|呀|啊|嘛|哦|吗|好吗|好不好|一下)+$",
                "",
                rest.strip(),
            )
            rest = rest.strip(" 　「」\"'《》")
            rest = re.sub(r"[。！!？?，,、；;：:…~～\s]+$", "", rest.strip())
            if len(rest) < 2 or rest in _GENERIC_SONG:
                break
            # 句子里抠出来的残片（带顿号/引号/超长）不可能是歌名
            if len(rest) > 20 or any(ch in rest for ch in "、，。；：！？\n"):
                break
            if rest.startswith(("的", "了", "吧", "吗", "呢")):
                break
            return rest
    # 「我想听《雨爱》」这种：触发词没命中，但书名号里就是歌名
    if wrapped:
        name = wrapped.group(1).strip()
        if len(name) >= 2 and name not in _GENERIC_SONG:
            return name
    return ""


# 主人话里出现这些，就直接开画，不等模型写标记
_DRAW_HINTS = (
    "画一张", "画一幅", "画个", "画一个", "画只", "画一只",
    "给我画", "帮我画", "画下", "画一下", "来张图", "来一张",
)


_STICKER_ASK_HINTS = (
    "表情包", "发个表情", "来张表情", "发张表情", "来点表情",
    "发个图", "来张图", "发张图", "斗图", "来点好玩的图",
)


# 这些说法本身就只在"要东西"的场景里出现，永远当成要表情包
_ALWAYS_STICKER_LINES = {
    "再一个", "再来一个", "再来几个", "再来点", "再来一张", "再来几张",
    "再发一个", "再发几个", "再发点", "继续发", "多来几张", "多来点",
    "还有吗", "还有没有", "不够",
}

# 这些要结合上下文（"我还要""还要"也可能是要吃饭）
_CONTEXT_STICKER_LINES = {"我还要", "还要", "再来", "多点"}


def wants_more_stickers(text: str, contextual: bool = False) -> bool:
    """接着上一条继续要表情包（"我还要""再来一个"这种）。

    只认整句：像"我还要吃饭"不能被当成要图。
    contextual=True 时连"我还要""还要"也算（需要调用方保证刚发过图）。
    """
    s = re.sub(r"[呀啊哦嘛啦吧呢～~!！。，,？?\s]", "", (text or "").strip())
    if s in _ALWAYS_STICKER_LINES:
        return True
    return contextual and s in _CONTEXT_STICKER_LINES


# 「自动回复」识别：对方号上挂着机器人，回它一句它就再回一句，会无限循环（踩过）
_AUTO_REPLY_PAT = re.compile(
    r"[\[【(（]\s*自动回复\s*[\]】)）]"      # [自动回复] 【自动回复】 (自动回复)
    r"|自动回复[:：]"                            # 自动回复：xxx
    r"|喵！.*毛线团"                             # 常见的猫娘自动回复文案
    r"|我(现在)?不在(哦|呀)?.*稍后回复",
)


def looks_auto_reply(text: str) -> bool:
    """这条消息看着像对方的自动回复吗。"""
    body = (text or "").strip()
    if not body:
        return False
    return bool(_AUTO_REPLY_PAT.search(body))


def wants_sticker(text: str) -> bool:
    """对方是不是明确在要表情包。"""
    candidate = (text or "").strip()
    if not candidate or len(candidate) > 30:
        return False
    return any(hint in candidate for hint in _STICKER_ASK_HINTS)


def wants_draw(text: str) -> bool:
    candidate = (text or "").strip()
    return any(hint in candidate for hint in _DRAW_HINTS)


def wants_find_image(text: str) -> bool:
    candidate = (text or "").strip()
    return any(hint in candidate for hint in _FINDIMG_HINTS)


# 要等一会儿才能回话时，先说这些垫一句
_WAIT_LINES = ("我看看啊", "等一下哦，我瞄一眼", "等我一下，我看看")

# 她申请去看视频时说的话
_ASK_LINES = (
    "爸爸，我今天可以去B站看个视频吗？",
    "诶，我今天想去B站逛逛，可以吗？",
    "爸爸～米米今天想去看个视频，你同意嘛",
)

# 主人叫她自己去看时，她的反应
_GO_WATCH_LINES = (
    "好呀，那我挑一个去啦",
    "那我找找看嘿嘿",
    "嗯！我去挑一个好看的",
)

# 判断主人是答应还是拒绝
_AFFIRM = ("可以", "同意", "好啊", "好呀", "行", "去吧", "批准", "准了", "看吧", "去看")
_NEGATIVE = ("不行", "不要", "算了", "别去", "不准", "不可以", "下次", "先别")

# 唱到一半，主人要不要下一段。跟"批准看视频"那套分开，
# 因为"我还想继续听"里有"想"但不在 _AFFIRM 里（踩过）
_SING_STOP = (
    "不听了", "不想听", "不要了", "不用了", "算了", "别唱", "不继续",
    "停", "够了", "到此", "不好", "不听",
)
_SING_MORE = (
    "继续", "接着", "还要", "想听", "还想", "再唱", "下一段", "下一首",
    "后面", "往下", "接着唱", "听", "要", "好", "嗯", "来", "想看",
)


def looks_new_song_request(text: str) -> bool:
    """这句是在点一首**新的**歌吗（"我要听你唱宝船"）。

    为什么单独一个函数：`_SING_MORE` 里有"听""要"这种单字，
    「我要听你唱宝船」会被 wants_more_song 当成"继续放上一首"，
    结果一直发原曲的分段，永远点不到新歌（2026-10-30 实测踩到）。
    """
    body = (text or "").strip()
    if not body or len(body) > 20:
        return False
    return wants_sing(body) and bool(pick_song_name(body))


def wants_more_song(text: str) -> bool:
    """主人说"继续听"之类。注意：点新歌不算"继续"。"""
    candidate = (text or "").strip()
    if not candidate or len(candidate) > 15:
        return False
    if any(word in candidate for word in _SING_STOP):
        return False
    if looks_new_song_request(candidate):
        return False
    return any(word in candidate for word in _SING_MORE)


def wants_stop_song(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate or len(candidate) > 15:
        return False
    return any(word in candidate for word in _SING_STOP)

# 主人直接叫她自己去看视频
_WATCH_HINTS = (
    "找个视频", "找个片子", "看个视频", "自己去看", "自己找", "去B站看",
    "看点视频", "随便看看视频", "去看视频", "找点视频", "看个片", "看会儿视频",
    "去逛逛B站", "去B站逛", "挑个视频", "找视频看",
    "换个视频", "换一个视频", "再找一个", "再看一个", "别的视频", "其他视频",
    "换一个看", "再找找",
)

# 模型自己判断"主人在让我去看视频"时写这个标记
_WATCH_RE = re.compile(r"\[\[看片\]\]")


def wants_watch_video(text: str) -> bool:
    """主人直接叫她自己去看，不用再走申请流程。

    关键词只是兜底；真正的判断交给模型——它会写 [[看片]] 标记。
    中文说法太自由了，光靠关键词会漏掉一大半。
    """
    candidate = (text or "").strip()
    if not candidate:
        return False
    if any(h in candidate for h in _WATCH_HINTS):
        return True
    # 「换个别的视频看看」这类没命中固定词，但意思很明确
    if "视频" in candidate and any(k in candidate for k in ("换", "再", "别的", "其他")):
        return "看" in candidate or "找" in candidate
    return False


def _owner_key(cid) -> str:
    """把会话 ID 归一成纯 QQ 号。

    私聊会话的 cid 是 "u1000000001"，群聊是 "g123456"，
    而 OWNER_QQS 里是整数 1000000001。统一成纯数字才能对上，
    否则"最后活跃时间""许可申请""待回复登记"这些表全部查不到。
    """
    return re.sub(r"^[ug]", "", str(cid))


def _group_lines(text: str, per: int = 3) -> list[str]:
    """把多行报告按 N 行合成一条消息，别一行刷一条。"""
    raw = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    return ["\n".join(raw[i : i + per]) for i in range(0, len(raw), per)]


def strip_last_period(text: str) -> str:
    """最后一句不留句号——发消息带句号看着太正式。"""
    lines = (text or "").split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            lines[i] = re.sub(r"[。．.]\s*$", "", lines[i].rstrip())
            break
    return "\n".join(lines)


def ensure_bubbles(text: str, limit: int = 3) -> str:
    """她爱把两句话挤在一行。只有一行时按句号拆开，凑成 2~3 条气泡。"""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    if len(lines) != 1:
        return text
    only = lines[0]
    parts = [seg.strip() for seg in re.split(r"(?<=[。！？!?])\s*", only) if seg.strip()]
    if len(parts) < 2:
        return text
    merged: list[str] = []
    for part in parts:
        if merged and len(part) <= 2:
            merged[-1] += part
        else:
            merged.append(part)
    return "\n".join(merged[:limit])


def strip_markers(text: str) -> str:
    """把模型写的各种标记全部抠掉。

    搜索成功后会再调一次模型复述结果，那一次它常常又写一遍标记，
    所以每个出口都要过一遍这个函数，不能只在第一次清理。
    """
    for pattern in (
        _SEARCH_RE, _DRAW_RE, _FINDIMG_RE, _STICKER_RE, _SHOT_RE, _WATCH_RE,
        _SING_RE, _VOICE_RE, _SONGLIST_RE, _INTROSPECT_RE,
    ):
        text = pattern.sub("", text)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return "\n".join(lines).strip()


def clean_output(text: str) -> str:
    text = text.replace("\r\n", "\n").strip()
    for pattern, repl in _CLEANERS:
        text = pattern.sub(repl, text)
    return text.strip()


# 模型用这个标记表示"这句想用声音说出来"，写成 [[语音]]
_VOICE_RE = re.compile(r"\[\[语音\]\]")

# 模型用这个标记表示"我要把曲库全清单报出来"，写成 [[歌单]]
_SONGLIST_RE = re.compile(r"\[\[歌单\]\]")

# 模型用这个标记表示"我要查服务器里的东西"，写成 [[查:歌单]] / [[查:说说]]
_INTROSPECT_RE = re.compile(r"\[\[查[:：]\s*([^\[\]]{1,20})\]\]")

# 跟朋友说话时绝对不能出现的词（出现了就重写，再不行就删句）
_FRIEND_LEAK_RE = re.compile(r"爸爸|主人|你爸")

# 主人问这些，直接去查真实数据，不等模型自觉写标记（她经常只嘴上说"我去查"）
_INTROSPECT_HINTS = (
    ("歌单", ("歌单", "曲库", "几首", "多少首", "都会唱", "会唱什么", "会哪几首")),
    ("说说", ("说说", "空间", "发了几条", "发了多少", "发过什么")),
    # 「记得」「记住」太容易误伤（「你不记得了吗」也算），只留明确在问记忆的
    ("记忆", ("你的记忆", "记忆里", "记得多少", "记住多少", "聊过多少", "聊了多少", "存了多少")),
    ("性格", ("性格", "你最近的变化", "你变了", "心情怎么样")),
    ("状态", ("你的状态", "运行状态", "状态怎么样", "系统状态", "一切正常吗")),
    ("能力", ("会什么", "能做什么", "有什么功能", "会哪些")),
    ("环境", ("环境", "系统", "服务器", "内存", "磁盘", "负载", "cpu", "跑得动", "硬件")),
    ("日志", ("日志", "报错", "出错", "错误", "崩了", "异常")),
    ("文件", ("文件", "占用", "占多大", "多大空间", "存了多少", "数据目录")),
    ("服务", ("服务", "线路", "容器", "中继", "进程", "在线状态")),
    ("素材", ("素材", "家底", "存了哪些", "有什么图", "多少张图")),
    ("总览", ("全部", "总览", "全查", "所有情况", "整体情况")),
    ("数据", ("数据目录", "服务器上有", "存了些什么", "文件")),
)


# 她说"自检"就真去试一遍自己的能力
_SELFCHECK_HINTS = ("自检", "检查一下你自己", "检查你自己", "自己检查一下", "做个自检")


def wants_selfcheck(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate or len(candidate) > 20:
        return False
    return any(word in candidate for word in _SELFCHECK_HINTS)


def wants_introspect(text: str) -> str:
    """主人是不是在问"你自己"的情况。返回主题，没有就返回空。"""
    candidate = (text or "").strip()
    if not candidate or len(candidate) > 30:
        return ""
    for topic, words in _INTROSPECT_HINTS:
        if any(word in candidate for word in words):
            return topic
    return ""


# 用户明确要语音时才发，不要自作主张
_VOICE_HINTS = (
    "发语音", "发个语音", "发条语音", "来条语音", "来段语音",
    "语音回", "语音说", "用语音", "说句话", "说一句", "声音给我",
    "念一下", "念给我", "读一下", "读给我", "说给我听", "让我听听",
    "开个口", "出声", "你的声音", "声音什么样", "声音好听",
    "讲给我听", "唱给我听", "我想听你", "听你说话",
)


def wants_voice(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate or len(candidate) > 40:
        return False
    # 只要提到「语音/声音」并且带着「发/说/听/来/给/想」这类动作，就当成要她开口——
    # 之前只列固定说法，结果「发句语音」这种变化没命中（踩过）。
    # 加动作词是为了别把「聊聊语音识别技术」当成点播
    if ("语音" in candidate or "声音" in candidate) and any(
        verb in candidate for verb in ("发", "说", "听", "来", "给", "想", "让")
    ):
        return True
    return any(hint in candidate for hint in _VOICE_HINTS)


def split_message(text: str, limit: int) -> list[str]:
    """按段落/句子切开，保证每段不超过 limit。"""
    if limit <= 0 or len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    buf = ""
    for para in text.split("\n"):
        piece = para if not buf else "\n" + para
        if len(buf) + len(piece) <= limit:
            buf += piece
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(para) <= limit:
            buf = para
            continue
        # 单段就超长了，找句子边界硬切
        rest = para
        while len(rest) > limit:
            cut = max((rest.rfind(ch, 0, limit) for ch in _SENTENCE_END), default=-1)
            if cut < limit // 3:
                cut = limit - 1
            chunks.append(rest[: cut + 1])
            rest = rest[cut + 1 :]
        buf = rest
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?…~])|\n+")

# 真人把话拆成几条发，不会一整段怼过来
_BUBBLE_CHOICES = (1, 2, 2, 3, 3)


def plan_bubbles(text: str, limit: int) -> list[str]:
    """把回复规划成几条消息。

    模型用换行分隔不同的意思时，就一行发一条——这样每条是独立的一句话，
    而不是把一整段硬切开。没有换行才退回按句子切。
    """
    text = text.strip()
    if not text:
        return []

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) > 1:
        bubbles: list[str] = []
        for line in lines:
            bubbles.extend(split_message(line, limit))
        # 条数太多会像刷屏，把尾巴合并成一条
        if len(bubbles) > config.MULTI_BUBBLE_MAX:
            head = bubbles[: config.MULTI_BUBBLE_MAX - 1]
            head.append("".join(bubbles[config.MULTI_BUBBLE_MAX - 1 :]))
            bubbles = head
        return bubbles

    if len(text) <= 18:  # 太短就整条发，「嗯」分成两条会很怪
        return [text]

    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p and p.strip()]
    if len(parts) <= 1:
        return split_message(text, limit)

    target = min(len(parts), random.choice(_BUBBLE_CHOICES))
    if target <= 1:
        return split_message(text, limit)

    groups: list[list[str]] = [[] for _ in range(target)]
    for index, part in enumerate(parts):
        groups[index * target // len(parts)].append(part)

    chunks: list[str] = []
    for group in groups:
        if group:
            chunks.extend(split_message("".join(group), limit))
    return chunks or [text]


_PAREN_RE = re.compile(r"[（(]([^（）()]{0,40})[）)]")


def strip_actions(text: str) -> str:
    """删掉括号里的动作描写。

    模型很难靠 prompt 戒掉这个，因为它自己的历史里全是。所以兜一层硬过滤：
    短的、不含数字的括号内容一律去掉（闲聊里这种括号基本就是动作描写），
    含数字的保留，因为多半是正经补充说明。
    """
    def _repl(match: re.Match) -> str:
        inner = match.group(1).strip()
        if not inner:
            return ""
        if any(ch.isdigit() for ch in inner) or len(inner) > 24:
            return match.group(0)
        return ""

    cleaned = _PAREN_RE.sub(_repl, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class WhaleBot:
    def __init__(self):
        self.ob = onebot.OneBotClient(config.ONEBOT_API_BASE, config.ONEBOT_TOKEN)
        self.mem = memory.Memory(
            config.MEMORY_FILE, config.HISTORY_TURNS, config.MAX_HISTORY_CHARS
        )
        self.persona = config.load_persona()
        # 朋友用另一套人设：不知道"爸爸"这个人存在，从根上断掉间接泄漏
        friend_file = config.BASE_DIR / "persona_friend.md"
        self.persona_friend = (
            friend_file.read_text(encoding="utf-8")
            if friend_file.exists()
            else self.persona
        )
        # 挂着的加好友申请：{qq: flag}，主人可以事后 /同意 QQ 补批
        self._pending_add: dict[int, str] = {}
        self._pending_order: list[int] = []
        # 对方发过的表情包/照片计数：{cid: {"sticker": n, "photo": n, "ts": 时间}}
        # 她说话必须有依据，不能张口就"你发的表情包真有意思"
        self._media_seen: dict[str, dict] = {}
        # 上次给她这边发过表情包的时间：{cid: ts}，用来判断"我还要"是不是在追图
        self._last_sticker_at: dict[str, float] = {}
        # 已经提醒过"对方没加我"的人：{qq:日期}
        self._unreachable_told: set[str] = set()
        # 神经元记忆：连接 + 每人待抽取的对话缓冲
        self._nconn = None
        self._neuron_slot: dict[str, dict] = {}
        # 合并等待：{cid: {texts, images, task, first, last, use_voice}}
        self._merge: dict[str, dict] = {}

        # 正在做/排队的点歌数，用来告诉别人要等多久
        self._sing_inflight = 0
        # 朋友的称呼记录：{qq: {"name": "小明", "asked": 时间戳, "awaiting": false}}
        self._contacts = self._load_contacts()
        self.me: dict | None = None

        # 消息去重：NapCat 重连或重试时不至于回两遍
        self._seen: set = set()
        self._seen_order: deque = deque(maxlen=800)

        # 同一会话串行，保证回复顺序不乱
        self._locks: dict[str, asyncio.Lock] = {}

        # 每个会话最后一次互动的时间，用来判断该不该主动开口
        self._last_active: dict[str, float] = {}
        # 这一轮不许回表情包的人（他这条消息里已经带了表情包）
        self._suppress_sticker: set[str] = set()
        # 这次开口距上次聊了多久（秒）。超过阈值就提示她换话题
        self._topic_gap: float = 0.0
        # 发完消息等回复的登记表：{cid: {due, left, sent}}
        self._pending: dict[str, dict] = {}
        # 看视频申请：{cid: {"state": waiting/watching, "asked": 时间}}
        self._permit: dict[str, dict] = {}
        # 每天问过一次就不再问：{cid: "2026-10-24"}
        self._watch_asked: dict[str, str] = {}
        # 今天的申请计划：{cid: {"date","hours","asked"}}
        self._watch_plan: dict[str, dict] = {}
        # 今天的说说计划：{"date","hours","done"}
        self._qzone_plan: dict[str, dict] = {}
        # 最近发过的说说内容，用来避免重复
        self._qzone_recent: list[str] = []
        # 她的性格快照（会随对话慢慢变）
        self.evolve_note = evolve.load()
        # 聊了多少轮，用来触发性格演化
        self._turn_count = 0
        # 正在看视频（下载/转写中）：这时候别让她假装看完了
        self._watching: dict[str, bool] = {}
        # 正在唱：别同时唱两首
        self._singing: dict[str, bool] = {}
        # 唱到一半等主人决定"要不要下一段"：{cid: {query, next, total, title}}
        self._song_session: dict[str, dict] = {}
        # 最近一次发出的歌：{qq: {query,title,index,total,ts}}
        # 进程重启后「继续」也能接上（2026-10-30 加）
        self._last_song: dict[str, dict] = {}
        # 行动台账：{qq: {动作: 时间戳}}。她只能说这里真有的（2026-10-30 加）
        self._actions: dict[str, dict] = {}
        # 朋友的时间窗口限流：{cid: [最近几条消息的时间戳]}
        self._friend_hits: dict[str, deque] = {}
        # 朋友今天点了几首歌：{cid: {"date": "...", "count": n}}
        self._friend_songs: dict[str, dict] = {}
        # 最近看过的视频地址，避免重复看同一个：{cid: [url, ...]}
        self._watched_urls: dict[str, list] = {}
        # 上次把二维码发到邮箱的时间，防止刷屏
        self._last_qr_mail = 0.0
        self._load_state()
        self._tasks: set[asyncio.Task] = set()

    # ---------- 小工具 ----------
    def _remember_seen(self, key) -> bool:
        if key in self._seen:
            return False
        if len(self._seen_order) == self._seen_order.maxlen:
            self._seen.discard(self._seen_order[0])
        self._seen.add(key)
        self._seen_order.append(key)
        return True

    def _lock(self, cid: str) -> asyncio.Lock:
        lock = self._locks.get(cid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[cid] = lock
        return lock

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _recover_with_qr(self, force: bool = False, restart: bool = True) -> None:
        """掉线后自动重启 NapCat、把新二维码发到邮箱。

        这样人在外面也能扫码恢复，不用回电脑边。
        force=True 表示是主人发邮件主动要的，不受半小时限制。
        """
        now = time.time()
        if not force and now - self._last_qr_mail < 1800:  # 半小时内只发一次
            log.info("二维码刚发过，这次不重复发")
            return
        self._last_qr_mail = now

        if not restart or not config.NAPCAT_MANAGED:
            # 不重启：直接把现有的二维码发出去（NapCat 登不上时会自己刷新它）
            log.info("掉线了但先不重启，只把二维码发到邮箱")
            path = config.QRCODE_PATH
            if path.exists() and (time.time() - path.stat().st_mtime) < 150:
                # 二维码是新鲜的（NapCat 刚生成），直接当附件发出去
                await asyncio.to_thread(
                    notify.send_mail,
                    "小鲸鱼：登录二维码（%s）" % time.strftime("%m-%d %H:%M"),
                    "NapCat 掉线了，这是刚生成的二维码，两分钟内有效。\n"
                    "用另一块屏幕显示出来，再用手机摄像头扫。\n",
                    path,
                )
            else:
                await asyncio.to_thread(
                    notify.notify_offline,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    "QQ 未登录（二维码已过期）。如果半小时内自己没回来，"
                    "看门狗会重启一次 NapCat 生成新码",
                )
            return

        try:
            done = await asyncio.to_thread(
                subprocess.run,
                ["sudo", "-n", "systemctl", "restart", "napcat"],
                capture_output=True,
                timeout=90,
            )
            if done.returncode != 0:
                detail = (done.stderr or b"").decode("utf-8", "replace")[:200]
                log.warning("自动重启 NapCat 失败：%s", detail)
                return
            log.info("已自动重启 NapCat，等它生成二维码")
        except Exception as exc:
            log.warning("自动重启 NapCat 出错：%s", exc)
            return

        # NapCat 起来到生成二维码大概十几秒
        await asyncio.sleep(20)
        path = config.QRCODE_PATH
        if not path.exists():
            log.warning("没找到二维码文件：%s", path)
            return

        # 二维码只有新生成的时候才有意义。文件太旧说明它压根没要扫码
        # （NapCat 用快速登录票据自己登录回来了），发旧图会误导人。
        age = time.time() - path.stat().st_mtime
        if age > 120:
            log.info("二维码是 %.0f 秒前的旧文件，说明已经自动登录，不发", age)
            when = time.strftime("%Y-%m-%d %H:%M:%S")
            if force:
                await asyncio.to_thread(
                    notify.send_mail,
                    "小鲸鱼：不用扫码了",
                    f"时间：{when}\n\n"
                    "我重启之后用记住的登录状态自己回来了，没有生成新二维码。\n"
                    "现在应该能正常跟我说话。\n\n"
                    "只有真的被腾讯踢下线时，快速登录才会失效，\n"
                    "那时候重启就会生成二维码，我会自动发给你。\n\n"
                    "—— 小鲸鱼",
                )
            return

        when = time.strftime("%Y-%m-%d %H:%M:%S")
        if force:
            sent = await asyncio.to_thread(
                notify.notify_qrcode_requested, when, "（你发邮件要的）"
            )
        else:
            sent = await asyncio.to_thread(notify.notify_qrcode, when)
        log.info("二维码邮件%s", "已发出" if sent else "发送失败")

    async def _mail_trigger_loop(self) -> None:
        """每 60 秒看一次邮箱：主人发主题带「扫码」的邮件，就给他发新二维码。"""
        if not config.MAIL_IMAP_ENABLED:
            return
        log.info("邮箱监听已启用：发主题带「扫码」的邮件就能要到二维码")
        while True:
            await asyncio.sleep(60)
            try:
                subject = await asyncio.to_thread(notify.check_trigger_mail)
            except Exception as exc:
                log.warning("查邮箱出错：%s", exc)
                continue
            if not subject:
                continue
            log.info("主人发邮件要二维码了：%s", subject[:40])

            # 本来就好的话别乱重启，那会把自己踢下线
            force = "强制" in subject
            try:
                online = await self.ob.get_status()
            except onebot.OneBotError:
                online = False
            if online and not force:
                log.info("当前在线，不用发二维码，回信说明一下")
                when = time.strftime("%Y-%m-%d %H:%M:%S")
                await asyncio.to_thread(
                    notify.send_mail,
                    "小鲸鱼：我现在好好的",
                    f"时间：{when}\n\n"
                    "收到你的扫码请求，但我现在是在线的，不用扫码。\n"
                    "如果 QQ 里收不到我说话，再发一封邮件过来。\n\n"
                    "—— 小鲸鱼",
                )
                continue

            self._spawn(self._recover_with_qr(force=True))

    async def _watchdog(self) -> None:
        """每 5 分钟摸一次 NapCat。

        处置策略（用户要求"不轻易重启"）：
        1. 连续两次失败才算掉线，立刻发邮件（附二维码）——**不重启**；
        2. 只有 NapCat 的 HTTP 口彻底连不上（进程死了），并且已经持续
           WATCHDOG_RESTART_AFTER_MINUTES 分钟，才重启一次；
        3. 两次重启之间至少隔 WATCHDOG_RESTART_COOLDOWN_MINUTES 分钟，
           免得反复重启把"快速登录"的路打断，反而必须扫码；
        4. 恢复后照常发上线邮件。
        """
        interval = config.WATCHDOG_SECONDS
        if interval <= 0:
            return
        down = False
        misses = 0
        api_dead_since = 0.0
        last_restart = 0.0

        while True:
            await asyncio.sleep(interval)
            reason = None
            api_dead = False
            try:
                online = await self.ob.get_status()
                if not online:
                    reason = "NapCat 活着，但 QQ 未登录（要扫码）"
            except onebot.OneBotError as exc:
                api_dead = True
                reason = f"连不上 NapCat：{exc}"

            if reason:
                misses += 1
                if api_dead and not api_dead_since:
                    api_dead_since = time.time()
                if not api_dead:
                    api_dead_since = 0.0

                if misses >= 2 and not down:
                    down = True
                    when = time.strftime("%Y-%m-%d %H:%M:%S")
                    log.warning("【掉线】%s（%s）", reason, when)
                    await asyncio.to_thread(notify.notify_offline, when, reason)
                    # 先把二维码发到邮箱，**不重启**
                    self._spawn(self._recover_with_qr(restart=False))
                    continue

                # 进程真的死了，且拖了很久 → 重启一次
                if down and api_dead and api_dead_since:
                    waited = time.time() - api_dead_since
                    cooldown = config.WATCHDOG_RESTART_COOLDOWN_MINUTES * 60
                    limit = config.WATCHDOG_RESTART_AFTER_MINUTES * 60
                    if not config.NAPCAT_MANAGED:
                        # 现在用 SnowLuma 容器托管 QQ，重启 napcat 会把号抢回来
                        continue
                    if waited >= limit and (time.time() - last_restart) >= cooldown:
                        last_restart = time.time()
                        log.warning(
                            "NapCat 已失联 %.0f 分钟，重启一次（之后不再自动重启）",
                            waited / 60,
                        )
                        self._spawn(self._recover_with_qr(restart=True))
                continue

            misses = 0
            api_dead_since = 0.0
            if down:
                down = False
                when = time.strftime("%Y-%m-%d %H:%M:%S")
                log.info("【恢复】QQ 重新上线（%s）", when)
                await asyncio.to_thread(notify.notify_online, when)

    # ---------- 发消息 ----------
    async def send_segments(self, mtype, user_id, group_id, segments):
        """返回 NapCat 的响应（dict），失败返回 None。"""
        try:
            if mtype == "group":
                data = await self.ob.send_group(group_id, segments)
            else:
                data = await self.ob.send_private(user_id, segments)
            return data if isinstance(data, dict) else {}
        except onebot.OneBotError as exc:
            log.error("发送失败：%s", exc)
            await self._on_send_failed(mtype, user_id, group_id, str(exc))
            return None

    # ---------- 神经元记忆：定时维护 ----------

    def _dream_file(self):
        return config.DATA_DIR / "neuron_dream.json"

    DREAM_MENTION_RATE = 0.25     # 只有这个概率的日子会顺口提一句（大约一周两次）

    def _save_dream(self, texts: list[str], line: str = "", mention: bool = True) -> None:
        try:
            self._dream_file().write_text(
                json.dumps(
                    {
                        "date": time.strftime("%Y-%m-%d"),
                        "texts": texts,
                        "line": line or "、".join(texts),
                        "mention": mention,
                        "used": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            log.debug("梦记不下来：%s", exc)

    def _fresh_dream(self, for_mention: bool = True) -> dict:
        """今天凌晨做的梦。for_mention=True 只返回"今天允许拿出来说"的那种。

        用户要求：不用每天都说梦，偶尔提一次就够。
        """
        try:
            data = json.loads(self._dream_file().read_text(encoding="utf-8"))
        except Exception:
            return {}
        if data.get("used") or data.get("date") != time.strftime("%Y-%m-%d"):
            return {}
        if for_mention and not data.get("mention", True):
            return {}
        texts = [t for t in (data.get("texts") or []) if t]
        if not texts:
            return {}
        return {"line": data.get("line") or "、".join(texts), "texts": texts}

    def _mark_dream_used(self) -> None:
        try:
            path = self._dream_file()
            data = json.loads(path.read_text(encoding="utf-8"))
            data["used"] = True
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    async def _compose_dream(self, texts: list[str]) -> str:
        """把浮上来的几条记忆，揉成半梦半醒的一句话。失败就退回"列举"。"""
        prompt = (
            "你在半梦半醒之间，脑子里浮起这几件事：\n"
            + "\n".join(f"- {t}" for t in texts)
            + "\n\n用**一句话**（不超过 25 字）把它们揉成一个模糊的念头，像梦话。\n"
            "要求：不要列点、不要说「我梦到」「我梦见」，不要解释，不要加引号。\n"
            "只输出那一句话。"
        )
        try:
            raw = await llm.chat(
                [{"role": "user", "content": prompt}], temperature=1.1, max_tokens=80
            )
        except Exception as exc:
            log.debug("梦话没编出来：%s", exc)
            return "、".join(texts)
        line = re.sub(r"[\r\n]+", " ", str(raw or "")).strip().strip('"“”')
        return line[:40] or "、".join(texts)

    async def _neuron_maint_loop(self) -> None:
        """每天凌晨 4 点：衰减 → 巩固 → 做梦（随机重组连线）。"""
        while True:
            now = time.localtime()
            target = time.mktime(
                (now.tm_year, now.tm_mon, now.tm_mday, 4, 0, 0, 0, 0, -1)
            )
            if target <= time.time():
                target += 86400
            wait = target - time.time()
            log.info("记忆维护将在 %.1f 小时后进行", wait / 3600)
            await asyncio.sleep(max(60, wait))
            conn = self._neuron_conn()
            if not conn:
                continue
            try:
                n = neuron.decay(conn)
                c = neuron.consolidate(conn)
                try:
                    idle = affection.decay()
                    if idle:
                        log.info("好感度：%d 人太久没说话，掉了点分", idle)
                except Exception as exc:
                    log.debug("好感度衰减失败：%s", exc)
                try:
                    n_mood = mood.decay_all()
                    if n_mood:
                        log.info("情绪：%d 人的情绪往平静回落了", n_mood)
                except Exception as exc:
                    log.debug("情绪回落失败：%s", exc)
                edges = neuron.replay(conn, picks=10)

                # 间隔重复：挑"快忘了但还有救"的记忆来复习（不是随机挑）
                owner_cid = f"u{next(iter(config.OWNER_QQS), 0)}"
                # 复习是纯数据库操作，不花 token：每晚多复习一些（上限 10），
                # 但只有前 3 条会写进"梦话"给你看。
                # 实测（90 天模拟）：3 条/晚 → 平均稳定性 12.6；10 条/晚 → 14.7，成本为零。
                due = neuron.due_reviews(conn, cid=owner_cid, limit=10)
                if due:
                    picked = [d["text"] for d in due[:3]]
                    for d in due:
                        neuron.hit(conn, d["id"])      # 复习一次 → 稳定性变长
                    conn.commit()
                    log.info(
                        "复习了 %d 条快忘的（R=%.2f~%.2f）",
                        len(due), due[0]["r"], due[-1]["r"],
                    )
                else:
                    rows = conn.execute(
                        "SELECT text FROM neuron WHERE cid = ? ORDER BY RANDOM() LIMIT 2",
                        (owner_cid,),
                    ).fetchall()
                    picked = [r["text"] for r in rows]
                line = ""
                if picked:
                    line = await self._compose_dream(picked)
                    # 只有部分日子会拿出来说，免得天天"我昨晚梦到…"
                    mention = random.random() < self.DREAM_MENTION_RATE
                    self._save_dream(picked, line, mention=mention)
                    log.info("今晚的梦%s拿出来说：%s", "会" if mention else "不", line)
                log.info(
                    "记忆维护完成：衰减 %d 条，巩固 %d 条，做梦连了 %d 条边，梦到：%s",
                    n, c, edges, " / ".join(picked),
                )
            except Exception as exc:
                log.warning("记忆维护失败：%s", exc)

    async def _neuron_shadow_loop(self) -> None:
        """每天 5 点跑一次影子对比，把结果追加到 data/shadow_daily.jsonl。"""
        while True:
            now = time.localtime()
            target = time.mktime(
                (now.tm_year, now.tm_mon, now.tm_mday, 5, 0, 0, 0, 0, -1)
            )
            if target <= time.time():
                target += 86400
            await asyncio.sleep(max(60, target - time.time()))
            try:
                import neuron_shadow

                res = await asyncio.to_thread(neuron_shadow.evaluate, 20, 120, 11)
                res["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
                with (config.DATA_DIR / "shadow_daily.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(res, ensure_ascii=False) + "\n")
                log.info("每日记忆对比：%s", res)
            except Exception as exc:
                log.warning("每日对比失败：%s", exc)

    # ---------- 神经元记忆 ----------

    def _neuron_conn(self):
        if getattr(self, "_nconn", None) is None:
            try:
                self._nconn = neuron.connect()
            except Exception as exc:
                log.warning("记忆库打不开：%s", exc)
                self._nconn = False
        return self._nconn or None

    def _neuron_recall(self, cid: str, text: str) -> list[str]:
        """顺着这句话想起几件相关的事。读不到就返回空，绝不影响正常回复。"""
        if not text or not str(text).strip():
            return []
        conn = self._neuron_conn()
        if not conn:
            return []
        try:
            hits = neuron.activate(conn, str(text)[:200], cid=cid, k=5)
        except Exception as exc:
            log.debug("记忆检索失败：%s", exc)
            return []
        if not hits:
            return []
        out = []
        for h in hits:
            out.append(f"· {h['text']}")
            try:
                neuron.hit(conn, h["id"], boost=0.04)
            except Exception:
                pass
        conn.commit()
        log.info("[%s] 想起 %d 件事：%s", cid, len(out), " / ".join(x[2:] for x in out)[:80])
        return out

    async def _neuron_learn(self, cid: str, user_text: str, reply_text: str) -> None:
        """把刚聊的这几句里"值得记住的"抽成记忆节点。

        节流：同一个人的对话攒够 4 句、且距上次至少 10 分钟，才抽一次。
        """
        conn = self._neuron_conn()
        if not conn:
            return
        now = time.time()
        slot = self._neuron_slot.setdefault(cid, {"lines": [], "last": 0.0})
        if user_text:
            slot["lines"].append("他：" + str(user_text)[:200].replace("\n", " "))
        if reply_text:
            slot["lines"].append("我：" + str(reply_text)[:200].replace("\n", " "))
        if len(slot["lines"]) < 4 or now - slot["last"] < 600:
            return

        chat = "\n".join(slot["lines"])
        slot["lines"] = []
        slot["last"] = now

        who = "爸爸" if is_owner(cid) else self._contact(cid).get("name") or "对方"
        prompt = (
            "从下面这段聊天里，抽出**值得长期记住**的内容。\n"
            "规则：只记稳定的事实、偏好、重要事件、情绪基调；\n"
            "不要记寒暄、不要记语气词、不要记临时的系统状态；\n"
            '"这个人"一律写「' + who + '」，她自己写「我」；不要编造。\n'
            "每条 20 字以内。没有值得记的就输出 []。\n"
            '输出纯 JSON 数组：[{"text": "...", "kind": "fact", "valence": 0.0}]\n'
            "kind 只能是 fact/prefer/event/mood/person，valence 是 -1 到 1。\n\n"
            + chat
        )
        try:
            raw = await llm.chat(
                [{"role": "user", "content": prompt}], temperature=0.3, max_tokens=600
            )
        except Exception as exc:
            log.debug("记忆抽取失败：%s", exc)
            return

        text = str(raw or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end < 0:
            return
        try:
            items = json.loads(text[start : end + 1])
        except Exception:
            return

        added = 0
        ids = []
        for it in items if isinstance(items, list) else []:
            fact = str(it.get("text", "")).strip()
            kind = str(it.get("kind", "fact")).strip()
            if not fact or kind not in neuron.TAU or len(fact) > 40:
                continue
            if re.search(r"报错|警告|日志|卡顿|崩溃|异常", fact):
                continue
            try:
                val = max(-1.0, min(1.0, float(it.get("valence", 0))))
            except Exception:
                val = 0.0
            try:
                ids.append(neuron.add_node(conn, fact, kind=kind, valence=val, cid=cid))
                added += 1
            except Exception:
                continue
        if len(ids) > 1:
            neuron.coactivate(conn, ids)
        conn.commit()
        if added:
            log.info("[%s] 新记住 %d 条", cid, added)

    async def _reply_to_sticker(self, cid, mtype, uid, group_id, mid=None) -> None:
        """对方只发了个表情包：问一句他怎么了，每次换个说法。"""
        if rules.no_sticker(cid):
            log.info("[%s] 他说过别发表情包，这条只回文字", cid)
        angles = (
            "问他是不是有什么事想说",
            "问他这是怎么了、心情是不是不太对",
            "让他有话直说，别光发表情",
            "问他发这个是想到什么了",
        )
        hint = (
            "对方只发来一个表情包，没有文字，图里是什么不重要。"
            f"{random.choice(angles)}。"
            "只说一两句，像随口一问；"
            "**不要描述表情包画面**，"
            "**也不要每次都写「怎么了」——换个说法**，别和上一条重样。"
        )
        try:
            msgs = self.build_messages(
                cid, "（对方只发来一个表情包，没有文字）", False, [], hint
            )
            raw = await llm.chat(msgs)
            text = strip_last_period(
                ensure_bubbles(strip_actions(clean_output(raw or "")))
            )
        except Exception as exc:
            log.warning("[%s] 问表情包失败：%s", cid, exc)
            text = ""

        # 模型没吐出来才退回兜底，保证不会一声不响
        if not text.strip():
            text = random.choice(("怎么啦", "这是怎么啦", "有事就说呀"))

        log.info("[%s] 只收到表情包，问一句：%s", cid, text.replace(chr(10), " / ")[:60])
        self.mem.append(cid, "assistant", text)
        for index, line in enumerate([x for x in text.split(chr(10)) if x.strip()]):
            if index:
                await asyncio.sleep(
                    random.uniform(config.BUBBLE_GAP_MIN, config.BUBBLE_GAP_MAX)
                )
            await self.send(mtype, uid, group_id, line)
        await self._compact(cid)

    async def _on_send_failed(self, mtype, user_id, group_id, message: str) -> None:
        """发不出去时判断原因。最常见的是"对方还没加我好友"，得告诉主人。"""
        if "好友" not in message or mtype != "private":
            return
        qq = int(user_id or 0)
        if not qq or qq in config.OWNER_QQS:
            return

        # 标记这个人暂时发不出去，主动消息别再试了
        cid = f"u{qq}"
        self._set_contact(cid, unreachable=True)
        log.info("[%s] 标记为暂时发不出去（他还没加我好友）", cid)

        # 一天只提醒主人一次，别刷屏
        key = f"{qq}:{time.strftime('%Y-%m-%d')}"
        if key in self._unreachable_told:
            return
        self._unreachable_told.add(key)
        for owner in config.OWNER_QQS:
            try:
                await self.send(
                    "private",
                    owner,
                    None,
                    f"{qq} 还没加我好友，我发不出消息哦\n"
                    "要么让他来加我，要么用 /删朋友 把他去掉",
                )
            except Exception:
                pass

    async def send(self, mtype, user_id, group_id, text, quote_id=None):
        await self.send_segments(
            mtype, user_id, group_id, onebot.text_message(text, quote_id)
        )

    async def send_voice(self, cid, text, mtype, uid, group_id, mid) -> None:
        """合成语音并发出去。发不成先换传递方式重试，最后退回文字。"""
        try:
            path = await voice.synthesize(text)
        except voice.TTSError as exc:
            log.error("[%s] 语音合成失败：%s", cid, exc)
            await self.send(mtype, uid, group_id, f"嗓子出问题了…（{exc}）")
            return

        log.info("[%s] 合成语音 %s（%.0f KB）", cid, path.name, path.stat().st_size / 1024)

        # 拿不到 message_id 就说明 NapCat 没真的发出去，换一种方式再试
        # 注意：语音不挂引用气泡。reply 段和 record 段混在一条消息里，
        # 部分 QQ 客户端会渲染成空气泡（踩过）。
        modes = [config.TTS_SEND_MODE] + [m for m in ("base64", "path") if m != config.TTS_SEND_MODE]
        for mode in modes:
            segments = onebot.voice_message(path, mode == "base64", None)
            result = await self.send_segments(mtype, uid, group_id, segments)
            log.debug("[%s] 语音发送(%s) 返回：%r", cid, mode, result)
            if result and result.get("message_id"):
                return
            log.warning("[%s] 语音用 %s 方式没拿到 message_id，换一种重试", cid, mode)

        log.error("[%s] 语音怎么都发不出去，退回文字", cid)
        await self.send(mtype, uid, group_id, text)

    # ---------- 事件入口 ----------
    async def handle_event(self, ev: dict) -> None:
        if not isinstance(ev, dict):
            return
        # 加好友 / 加群申请：以前直接忽略，导致朋友加了半天没通过（2026-10 补上）
        if ev.get("post_type") == "request":
            await self._on_request(ev)
            return

        if ev.get("post_type") != "message":
            return  # meta_event / notice 忽略

        self_id = ev.get("self_id")
        user_id = ev.get("user_id")
        if self_id is None or user_id is None or str(user_id) == str(self_id):
            return

        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return

        if uid not in config.ALLOWED_QQS:
            log.debug("忽略非白名单用户 %s", uid)
            return

        mid = ev.get("message_id")
        if mid is not None and not self._remember_seen(mid):
            log.debug("重复消息 %s，忽略", mid)
            return

        mtype = ev.get("message_type")
        group_id = ev.get("group_id")
        text = onebot.extract_text(ev.get("message"), self_id).strip()

        if mtype == "group":
            if not config.REPLY_TO_GROUPS:
                log.debug("群里不回复（REPLY_TO_GROUPS=false）")
                return
            if config.GROUP_WHITELIST and int(group_id or 0) not in config.GROUP_WHITELIST:
                return
            hits = onebot.mentioned_self(ev.get("message"), self_id)
            if not hits and not text.startswith(("小鲸鱼", "鲸鱼", "鱼鱼")):
                return  # 群里只接 @ 或点名
        elif mtype != "private":
            return

        cid = f"g{group_id}" if mtype == "group" else f"u{uid}"

        images = onebot.extract_images(ev.get("message"))

        # 表情包一律不回应（用户要求）：
        # 只有表情包 → 完全不理；表情包 + 文字 → 只处理文字，图丢掉。
        # 照片不受影响，照常看图。
        if images and onebot.images_all_stickers(ev.get("message")):
            # 系统会把图片换成「[图片]」占位符，光看 text 非空判断不出来
            plain = re.sub(r"\[(图片|表情|表情包)\]", "", text or "").strip()
            self._count_media(cid, "sticker")

            if not plain:
                # 只发了个表情包、没有文字：不分析图，但现编一句问他怎么了。
                # 不能背固定台词——用户嫌每次都是「怎么了呀」（踩过）
                self.mem.append(cid, "user", "（发来一个表情包）")
                await self._reply_to_sticker(cid, mtype, uid, group_id, mid)
                return

            # 表情包 + 文字：**这一轮不要再回他表情包**（用户 2026-11-01 要求）——
            # 他一条消息里已经把文字和图都给全了，再回个图等于自说自话。
            self._suppress_sticker.add(cid)
            text = plain
            if is_friend(cid):
                log.info("[%s] 朋友的表情包不看了，只回文字", cid)
                images = []

        if images:
            self._count_media(cid, "photo")

        # 他能发消息进来，说明加上了，把"发不出去"的标记清掉
        if is_friend(cid) and self._contact(cid).get("unreachable"):
            self._set_contact(cid, unreachable=False)
            log.info("[%s] 现在能发了，清掉不可达标记", cid)

        if not text and not images:
            await self.send(mtype, uid, group_id, "（收到一个我看不懂的消息…）")
            return
        if not text:
            text = "（发来一张图片）"

        if await self.handle_command(cid, text, mtype, uid, group_id):
            return

        use_voice = config.TTS_ENABLED and wants_voice(text)
        log.info(
            "[%s] %s：%s%s%s",
            cid,
            uid,
            text[:100],
            "  [要语音]" if use_voice else "",
            f"  [{len(images)} 张图]" if images else "",
        )
        self._enqueue_message(cid, text, mtype, uid, group_id, mid, use_voice, images)

    # ---------- 合并等待 ----------
    def _enqueue_message(self, cid, text, mtype, uid, group_id, mid, use_voice, images):
        """对方可能连着发好几句，先攒一下再一起回。

        每来一条新消息就把计时器重置；攒够 MERGE_MAX_SECONDS 就不再等。
        """
        key = _owner_key(cid)
        now = time.time()

        if not config.MERGE_ENABLED:
            self._spawn(
                self.reply(cid, text, mtype, uid, group_id, mid, use_voice, images)
            )
            return

        slot = self._merge.get(key)
        if slot is None:
            slot = {"texts": [], "images": [], "first": now, "use_voice": False}
            self._merge[key] = slot

        slot["texts"].append(text)
        if images:
            slot["images"] = images
        slot["use_voice"] = bool(slot.get("use_voice")) or bool(use_voice)
        slot["last"] = (cid, mtype, uid, group_id, mid)

        if slot.get("task") and not slot["task"].done():
            slot["task"].cancel()

        waited = now - float(slot["first"])
        if waited >= config.MERGE_MAX_SECONDS:
            delay = 0.0  # 攒太久了，立刻回
        else:
            delay = config.MERGE_WAIT_SECONDS
        slot["task"] = self._spawn(self._flush_merged(key, delay))

    async def _flush_merged(self, key: str, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # 又来了新消息，重新计时

        slot = self._merge.pop(key, None)
        if not slot or not slot.get("texts"):
            return

        texts = slot["texts"]
        merged = chr(10).join(texts)
        cid, mtype, uid, group_id, mid = slot["last"]
        images = slot.get("images") or None
        if len(texts) > 1:
            log.info("[%s] 合并了 %d 条再回：%s", cid, len(texts), merged[:80])
        await self.reply(
            cid, merged, mtype, uid, group_id, mid, bool(slot.get("use_voice")), images
        )

    # ---------- 指令 ----------
    # ---------- 好感度：保姆级管理（只有主人能用）----------

    _CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
               "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

    def _all_people(self) -> list[tuple[str, str]]:
        """白名单里所有人：(cid, 显示名)。"""
        out = []
        for qq in sorted(config.OWNER_QQS | config.FRIEND_QQS):
            if int(qq) <= 10000:
                continue
            cid = f"u{qq}"
            name = self._contact_name(cid) or ("主人" if is_owner(cid) else "")
            out.append((cid, name or f"还没问过名字（{qq}）"))
        return out

    def _resolve_person(self, arg: str) -> tuple[str, str]:
        """按 QQ 号或名字找人。返回 (cid, 错误提示)。找不到时 cid 为空、提示里带候选。"""
        raw = (arg or "").strip().replace("＠", "").replace("@", "").strip()
        if not raw:
            return "", "要查谁呀？写成「/查好感 1000000011」这样，或者直接写名字"
        digits = re.sub(r"\D", "", raw)
        if digits and len(digits) >= 5:
            cid = f"u{digits}"
            if cid in {c for c, _ in self._all_people()}:
                return cid, ""
            return "", (
                f"{digits} 不在白名单里。要加上他先发「/加朋友 {digits}」。\n"
                "现在名单里有：\n" + self._people_preview()
            )
        hits = [(c, n) for c, n in self._all_people() if raw and raw in n]
        if len(hits) == 1:
            return hits[0][0], ""
        if len(hits) > 1:
            return "", "有好几个人对得上：「" + "、".join(n for _, n in hits) + "」，你写清楚点，或者直接发 QQ 号"
        return "", (
            f"没找到「{raw}」这个人。\n现在名单里有：\n" + self._people_preview()
            + "\n也可以直接发 QQ 号"
        )

    def _people_preview(self) -> str:
        rows = []
        for cid, name in self._all_people():
            qq = str(_qq_of(cid))
            # 名字和 QQ 号都要给出来——只给名字的话，重名/没名字的时候没法操作
            shown = name.replace(f"（{qq}）", "").strip()
            rows.append(f"· {shown}（{qq}）  {affection.score(cid):.0f} 分")
        return "\n".join(rows) or "（空）"

    def _parse_delta(self, raw: str) -> tuple[float, str]:
        """把「+5」「-3」「5分」「五」都认出来。返回 (数值, 提示)。"""
        text = (raw or "").strip().replace("＋", "+").replace("－", "-").replace("分", "")
        if not text:
            return 0.0, ""
        sign = 1.0
        if text[0] in "+-":
            sign = -1.0 if text[0] == "-" else 1.0
            text = text[1:]
        note = ""
        try:
            value = float(text)
        except ValueError:
            if text in self._CN_NUM:
                value = float(self._CN_NUM[text])
                note = f"（「{text}」我按 {int(value)} 算了）"
            else:
                return 0.0, "分数得是数字呀，比如 /改好感 1000000011 +5"
        return sign * value, note

    # 关系是可以自由写的：女朋友/男朋友/妹妹/姐姐/老大…… 只按类别决定相处方式
    _RELATION_KINDS = (
        ("lover", ("女朋友", "男友", "男朋友", "女友", "恋人", "对象", "情侣",
                   "老公", "老婆", "未婚妻", "未婚夫")),
        ("family", ("妹妹", "姐姐", "哥哥", "弟弟", "女儿", "儿子")),
        # 只把"真的就是朋友"的放这一档。同学/同事/网友走 other + 预设，
        # 否则它们拿不到具体语气（同学不该用「您」，网友不该自来熟）。
        ("friend", ("朋友", "基友", "闺蜜")),
    )
    # 老版本存过英文，读的时候统一换回中文，别把 "friend" 当成一个自定义身份
    _LABEL_ALIASES = {"friend": "朋友", "lover": "恋人", "owner": "主人",
                      "family": "家人", "other": "朋友"}
    _FEMALE_HINTS = ("女", "妹", "姐", "妈", "婆", "娘", "姑", "婶", "母")
    _MALE_HINTS = ("男", "哥", "弟", "爸", "公", "叔", "爷", "兄", "父")

    def _kind_of_label(self, label: str) -> str:
        """把一个自由写的关系词归到一类：lover / family / friend / other。"""
        low = (self._LABEL_ALIASES.get((label or "").strip().lower())
               or (label or "")).strip().lower()
        if not low:
            return "friend"
        for kind, words in self._RELATION_KINDS:
            if any(w in low for w in words):
                return kind
        return "other"

    def _gender_of_label(self, label: str) -> str:
        """从关系词本身猜性别：女朋友→她，男朋友→他，猜不出返回空。"""
        for ch in (label or ""):
            if ch in self._FEMALE_HINTS:
                return "f"
            if ch in self._MALE_HINTS:
                return "m"
        return ""

    # 自定义身份的具体语气。光写"按这个身份相处"压不住她的默认性格，
    # 所以常见身份直接给：怎么说话 + 必须守住什么。
    _SOCIAL_PRESETS = (
        (("老师", "导师", "教授"), ("用「您」，说话收敛，多请教、汇报进度",
                                 "别撒娇、别用「宝宝」这类昵称、别提恋爱话题")),
        (("同学", "校友"), ("同龄人随口聊，可以吐槽作业和课表，可以约自习",
                          "别用「您」、别端着、别装乖")),
        (("同事", "工友"), ("同事之间那种得体，聊工作、吐槽加班",
                          "别撒娇、别叫老公老婆、别打听工资")),
        (("上司", "老板", "领导", "主管"), ("简短、尊重，先说结论，别绕",
                                        "别撒娇、别抱怨、别开玩笑过头")),
        (("邻居",), ("见面打招呼那种熟，可以聊天气、楼道、借还东西",
                   "别黏、别动不动说想你、别上门")),
        (("长辈", "叔叔", "阿姨", "爷爷", "奶奶"), ("有礼貌，关心身体，多听少说",
                                                "别用网络梗、别撒娇、别说教")),
        (("陌生人", "网友"), ("客气、简短、不主动打听隐私",
                           "别自来熟、别问住址电话、别默认很熟")),
        (("学长", "学姐"), ("带着一点仰慕但别过分，可以请教经验",
                          "别撒娇、别叫哥哥姐姐之外更亲的称呼")),
        (("学弟", "学妹"), ("热心一点，可以带一带、讲讲怎么混",
                          "别说教、别摆架子")),
    )

    def _social_preset(self, label: str):
        """按关系词给一套（语气, 必须守住），认不出来返回 None。"""
        text = (label or "").strip()
        if not text:
            return None
        for words, preset in self._SOCIAL_PRESETS:
            if any(w in text for w in words):
                return preset
        return None

    def _relation_admin(self, cid: str, arg: str) -> str:
        """主人给某个人设关系，关系词随便写。

        /设关系 QQ号 女朋友     ← 她认这个人做女朋友
        /设关系 QQ号 男朋友
        /设关系 QQ号 妹妹       ← 也能是亲情/其他身份
        /设关系 QQ号 老大
        /设关系 QQ号 恋人 她    ← 关系词不带性别时，可以补一个「她/他」
        /设关系 QQ号 朋友       ← 改回普通朋友
        """
        if not is_owner(cid):
            return "这个指令得主人自己来"
        bits = (arg or "").split()
        if len(bits) < 2:
            lines = ["现在的关系："]
            for person, name in self._all_people():
                qq = str(_qq_of(person))
                if is_owner(person):
                    rel = "主人（固定）"
                else:
                    rel = self._relation_label(person)
                lines.append(
                    f"· {(name or '还没问过名字').replace(f'（{qq}）', '')}"
                    f"（{qq}）  {rel}"
                )
            lines += [
                "",
                "随便写关系词，比如：",
                "  /设关系 QQ号 女朋友      /设关系 QQ号 男朋友",
                "  /设关系 QQ号 妹妹        /设关系 QQ号 老大",
                "  /设关系 QQ号 恋人 她     ← 关系词不带性别时补一个 她/他",
                "  /设关系 QQ号 朋友        ← 改回普通朋友",
            ]
            return "\n".join(lines)

        # 末尾的「她/他」可以当性别覆盖
        gender = ""
        if len(bits) >= 3 and bits[-1] in ("她", "女", "女生", "女孩子", "f"):
            gender = "f"
            bits = bits[:-1]
        elif len(bits) >= 3 and bits[-1] in ("他", "男", "男生", "男孩子", "m"):
            gender = "m"
            bits = bits[:-1]

        who, label = " ".join(bits[:-1]), bits[-1].strip("「」\"' ")
        person, err = self._resolve_person(who)
        if err:
            return err
        if is_owner(person):
            return "这是你自己，不用设——她一直叫你爸爸"

        if label in ("朋友", "普通朋友", "friend", "取消", "分手", "重置"):
            self._set_relation(person, "friend", "")
            name = self._contact_name(person) or str(_qq_of(person))
            return f"好，{name} 改回普通朋友了，她又会客气一点。"

        if not label or len(label) > 12:
            return "关系词没看懂，写短一点，比如：/设关系 1000000011 女朋友"

        kind = self._kind_of_label(label)
        if not gender:
            gender = self._gender_of_label(label)
        self._set_relation(person, label, gender)

        name = self._contact_name(person) or str(_qq_of(person))
        gtxt = {"f": "，她", "m": "，他"}.get(gender, "")
        extra = {
            "lover": "她会主动说想你、撒娇吃醋，被问「我们什么关系」也不会打岔了。",
            "family": "她会照这个身份相处，说话带点亲近的口气。",
            "other": "她会照这个身份称呼你、顺着相处。",
            "friend": "她还是普通朋友的语气。",
        }[kind]
        return (
            f"好，从现在起 {name} 是你的「{label}」{gtxt}。\n"
            f"{extra}\n"
            f"要改回来：/设关系 {_qq_of(person)} 朋友\n"
            f"要补性别：/设关系 {_qq_of(person)} {label} 她  （或 他）"
        )

    def _affection_admin(self, cid: str, cmd: str, arg: str) -> str:
        """处理 /查好感 和 /改好感 这一族。返回要发回去的话。"""
        if not is_owner(cid):
            return "这个指令我用不了，得主人自己来"

        # 只发 /查好感 不带人 → 全部列一遍
        if cmd in ("/查好感", "/看好感", "/好感名单", "/好感列表"):
            if not arg.strip():
                lines = ["现在的好感度："]
                for person, name in self._all_people():
                    value = affection.score(person)
                    lv, lv_name, _f, _n = affection.level_of(value)
                    tag = "（固定）" if affection.is_pinned(person) else ""
                    # 名字后面一律带 QQ 号：没名字的能认人，有名字的也能对上号
                    qq = str(_qq_of(person))
                    shown = name.replace(f"（{qq}）", "").strip()
                    lines.append(f"· {shown}（{qq}）  {value:.0f} 分 · Lv{lv} {lv_name}{tag}")
                lines.append("")
                lines.append("看某个人细节：/查好感 QQ号 或 名字")
                lines.append("增减：/改好感 QQ号 +5 或 -3")
                return "\n".join(lines)

            person, err = self._resolve_person(arg)
            if err:
                return err
            value = affection.score(person)
            lv, lv_name, _floor, nxt = affection.level_of(value)
            lines = [f"{self._contact_name(person) or _qq_of(person)} 的好感度 {value:.0f}/100 · Lv{lv} {lv_name}"]
            if affection.is_pinned(person):
                lines.append("这是主人，固定 100，改不动的")
                return "\n".join(lines)
            if nxt > value:
                lines.append(f"距下一级还差 {nxt - value:.0f} 分")
            due = [affection.UNLOCK[k][0] for k in affection.UNLOCK if value >= affection.UNLOCK[k][1]]
            locked = [f"{affection.UNLOCK[k][0]}（{affection.UNLOCK[k][1]}）"
                      for k in affection.UNLOCK if value < affection.UNLOCK[k][1]]
            if due:
                lines.append("已解锁：" + "、".join(due))
            if locked:
                lines.append("还锁着：" + "、".join(locked))
            rec = affection.record(person)
            if rec.get("streak"):
                lines.append(f"连续聊了 {rec['streak']} 天")
            events = (rec.get("events") or [])[-4:]
            if events:
                lines.append("最近变化：")
                for e in events:
                    lines.append(f"· {e.get('ts')} {e.get('why')} {float(e.get('d') or 0):+g}")
            lines.append("")
            lines.append("要改就发：/改好感 " + str(_qq_of(person)) + " +5（或 -3）")
            return "\n".join(lines)

        # /改好感 [人] [±N]
        parts = arg.split()
        want = 0.0
        note = ""
        person_arg = ""
        if len(parts) >= 2:
            person_arg, delta_arg = parts[0], parts[-1]
            want, note = self._parse_delta(delta_arg)
        elif len(parts) == 1:
            # 只给了一个词：可能是人，也可能是数字（此时只能查/上次那个人）
            only = parts[0]
            only_digits = re.sub(r"\D", "", only)
            if only_digits and len(only_digits) >= 5 and len(only) == len(only_digits):
                person, err = self._resolve_person(only)
                if err:
                    return err
                return (
                    f"要给 {self._contact_name(person) or only_digits} 加多少分呀？\n"
                    f"比如「/改好感 {only_digits} +5」，减就写 -3"
                )
            maybe, maybe_note = self._parse_delta(only)
            if maybe:
                return (
                    "要改谁呀？写成「/查好感」看名单，然后：\n"
                    "/改好感 QQ号 +5\n"
                    "/改好感 名字 -3"
                )
            person_arg = only
            return f"要给他加多少分呀？写成「/改好感 {only} +5」这样，负数就是减"
        else:
            return (
                "用法：\n"
                "/查好感 —— 看所有人\n"
                "/查好感 QQ号 —— 看某个人\n"
                "/改好感 QQ号 +5 —— 加 5 分\n"
                "/改好感 QQ号 -3 —— 减 3 分（也可以写名字）"
            )

        if want == 0:
            return "分数得是数字呀，比如「/改好感 1000000011 +5」。加就写 +5，减就写 -3"

        person, err = self._resolve_person(person_arg)
        if err:
            return err
        if affection.is_pinned(person):
            return "这是主人自己，固定 100 分，改不动呀"

        before = affection.score(person)
        affection.add(person, want, "主人手动调整", force=True)
        after = affection.score(person)
        lv_b, name_b, _a, _b = affection.level_of(before)
        lv_a, name_a, _c, _d = affection.level_of(after)

        lines = [
            f"{self._contact_name(person) or _qq_of(person)}：{before:.0f} → {after:.0f} 分"
            + (f"（{'加了' if want > 0 else '减了'} {abs(want):g}）" if want else "")
        ]
        if note:
            lines.append(note.strip("（）"))
        if lv_a != lv_b:
            lines.append(f"等级变了：Lv{lv_b} {name_b} → Lv{lv_a} {name_a}")
        for key, (label, need) in affection.UNLOCK.items():
            if before < need <= after:
                lines.append(f"解锁了「{label}」")
            elif after < need <= before:
                lines.append(f"锁上了「{label}」")
        if abs(want) > 30:
            lines.append("（这一下改挺多，别手抖呀）")
        return "\n".join(lines)

    # ---------- 指令清单 / 公告 ----------

    # (用法, 说明, 只有主人能用吗)
    COMMANDS = [
        ("/指令", "看这份清单（也可以发 /帮助 /功能 /菜单）", False),
        ("/记忆 关键词", "她脑子里跟这个词有关的记忆，不带关键词就看总览", False),
        ("/勿扰", "让她别主动找你（回消息不受影响）", False),
        ("/取消勿扰", "恢复正常", False),
        ("/勿扰状态", "看看现在是不是勿扰", False),
        ("/叫我 名字", "改她怎么称呼你（朋友也能用）", False),
        ("/好感度", "看现在多少分、解锁了什么、为什么涨", False),
        ("/心情", "看她现在什么情绪、为什么、怎么哄", False),
        ("/事件", "看她记下的事：几点别找、约好几点叫你", False),
        ("/设关系 谁 恋人", "把某个人设成她男朋友（恋人）；改回朋友就发 /设关系 谁 朋友", True),
        ("/查好感 [谁]", "看所有人的好感度；带 QQ 号或名字看细节", True),
        ("/改好感 谁 ±N", "手动加减某个人的好感度（例：/改好感 1000000011 +5）", True),
        ("/公告 内容", "给所有朋友发一条通知（发完报结果）", True),
        ("/加朋友 QQ号", "把某人加进白名单，对方再申请好友会自动通过", True),
        ("/删朋友 QQ号", "从白名单移除", True),
        ("/同意 QQ号", "通过待处理的好友申请（不发号码就同意最近一个）", True),
        ("/拒绝 QQ号", "拒绝好友申请", True),
        ("/朋友", "看白名单和最近活跃情况", True),
        ("/语音 文字", "用她的声音念出来（也能直接说「说句话给我听」）", True),
        ("/clear", "清空近期对话记忆", True),
        ("/status", "她现在的运行状态", True),
    ]

    def _commands_text(self, owner: bool) -> str:
        lines = ["我能用的指令："]
        for usage, desc, owner_only in self.COMMANDS:
            if owner_only and not owner:
                continue
            lines.append(f"{usage} —— {desc}")
        if owner:
            lines.append("")
            lines.append("（上面标着只有你能用的那几条，别人发会被回绝）")
        lines.append("")
        lines.append("不用打指令也能直接说：唱歌、看视频、画图、发表情、要语音。")
        return "\n".join(lines)

    def _last_announcement(self) -> str:
        path = config.DATA_DIR / "announcements.jsonl"
        try:
            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        except Exception:
            return "还没发过公告。用法：/公告 内容"
        if not rows:
            return "还没发过公告。用法：/公告 内容"
        last = rows[-1]
        return (
            f"上一条公告（{last.get('ts')}）：\n{last.get('text')}\n"
            f"结果：成功 {last.get('ok')} 个，失败 {last.get('fail')} 个\n"
            "要发新的就写「/公告 内容」"
        )

    async def _announce(self, cid, mtype, uid, group_id, text: str) -> None:
        """给所有朋友发同一条通知。隔几秒发一个，像人一样，别被风控。"""
        # 过滤掉 0 和明显不是 QQ 号的短号（.env 里可能残留脏数据）
        targets = [
            qq
            for qq in sorted(config.FRIEND_QQS)
            if qq not in config.OWNER_QQS and int(qq) > 10000
        ]
        body = clean_output(text) if "clean_output" in globals() else text
        ok, fail = [], []
        for index, qq in enumerate(targets):
            try:
                await self.send("private", str(qq), None, body)
                ok.append(qq)
                log.info("[公告] → %s 成功", qq)
            except Exception as exc:
                fail.append(qq)
                log.warning("[公告] → %s 失败：%s", qq, exc)
            if index + 1 < len(targets):
                await asyncio.sleep(random.uniform(3.0, 8.0))

        try:
            with (config.DATA_DIR / "announcements.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "text": body,
                            "ok": len(ok),
                            "fail": len(fail),
                            "ok_list": ok,
                            "fail_list": fail,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception as exc:
            log.debug("公告没记下来：%s", exc)

        note = f"公告发完了：成功 {len(ok)} 个"
        if fail:
            note += f"，失败 {len(fail)} 个（{', '.join(str(q) for q in fail)}）"
        if not targets:
            note = "白名单里还没有朋友，公告没发出去"
        await self.send(mtype, uid, group_id, note)

    def _neuron_report(self, cid: str, arg: str) -> str:
        conn = self._neuron_conn()
        if not conn:
            return "记忆库打不开"
        who = "爸爸" if is_owner(cid) else (self._contact(cid).get("name") or "你")
        if arg:
            hits = neuron.activate(conn, arg, cid=cid, k=6)
            if not hits:
                return f"想不起跟「{arg}」有关的事"
            lines = [f"跟「{arg}」有关的："]
            for h in hits:
                lines.append(f"· {h['text']}（{h['score']:.2f}）")
            return "\n".join(lines)

        st = neuron.stats(conn, cid=cid)
        lines = [f"关于{who}，我记着 {st['nodes']} 件事，互相连了 {st['synapses']} 条线"]
        for kind, info in sorted(st["by_kind"].items()):
            name = {
                "fact": "事实", "prefer": "喜好", "event": "发生过的事",
                "mood": "情绪", "person": "人",
            }.get(kind, kind)
            lines.append(f"· {name} {info['count']} 条")
        rows_all = conn.execute(
            "SELECT strength, stability FROM neuron WHERE cid = ?", (cid,)
        ).fetchall()
        if rows_all:
            rs = sorted(float(r["strength"]) for r in rows_all)
            mid = rs[len(rs) // 2]
            lines.append(
                f"记得最牢 {rs[-1]:.2f} · 中间 {mid:.2f} · 快忘了 {rs[0]:.2f}（1=刚想起来，0=想不起来）"
            )
        due = neuron.due_reviews(conn, cid=cid, limit=3)
        if due:
            lines.append(
                "该复习了：" + "；".join(f"{d['text']}（{d['r']:.2f}）" for d in due)
            )
        recent = conn.execute(
            "SELECT text FROM neuron WHERE cid = ? ORDER BY id DESC LIMIT 4", (cid,)
        ).fetchall()
        if recent:
            lines.append("最近记住的：" + "；".join(r["text"] for r in recent))
        fuzzy = conn.execute(
            "SELECT text, strength FROM neuron WHERE cid = ? ORDER BY strength ASC LIMIT 2",
            (cid,),
        ).fetchall()
        if fuzzy:
            lines.append(
                "快要想不起来的："
                + "；".join(f"{r['text']}（{r['strength']:.2f}）" for r in fuzzy)
            )
        dream = self._fresh_dream(for_mention=False) if is_owner(cid) else {}
        if dream:
            lines.append("昨晚的梦：" + dream["line"])
        lines.append("想查具体的就发 /记忆 关键词")
        return "\n".join(lines)

    async def handle_command(self, cid, text, mtype, uid, group_id) -> bool:
        head, _, arg = text.partition(" ")
        cmd = head.strip().lower()
        if cmd in ("/指令", "/功能", "/菜单", "/help", "/帮助", "/?", "/？"):
            await self.send(mtype, uid, group_id, self._commands_text(is_owner(cid)))
            return True
        if cmd in ("/公告", "/通知", "/broadcast"):
            if not is_owner(cid):
                await self.send(mtype, uid, group_id, "这个指令我用不了，得主人自己来")
                return True
            if not arg.strip():
                await self.send(mtype, uid, group_id, self._last_announcement())
                return True
            body = arg.strip()[:500]
            await self.send(
                mtype, uid, group_id,
                f"在发了，发给 {len([q for q in config.FRIEND_QQS if q not in config.OWNER_QQS and int(q) > 10000])} 个朋友，发完告诉你。",
            )
            self._spawn(self._announce(cid, mtype, uid, group_id, body))
            return True
        if cmd in ("/查好感", "/看好感", "/好感名单", "/好感列表",
                   "/改好感", "/加好感", "/减好感", "/调整好感"):
            actual = cmd
            if cmd in ("/加好感", "/减好感"):
                sign = "+" if cmd == "/加好感" else "-"
                bits = arg.split()
                if len(bits) >= 2:
                    arg = bits[0] + " " + sign + bits[-1].lstrip("+-")
                else:
                    arg = arg.strip()
            await self.send(mtype, uid, group_id, self._affection_admin(cid, actual, arg))
            return True
        if cmd in ("/设关系", "/关系", "/setrelation"):
            await self.send(mtype, uid, group_id, self._relation_admin(cid, arg))
            return True
        if cmd in ("/叫我", "/称呼", "/叫我吧", "/rename"):
            want = arg.strip().strip("「」\"'“” ")
            if not want:
                now = self._contact_name(cid)
                hint = f"现在叫你「{now}」。" if now else "还不知道该怎么叫你。"
                await self.send(
                    mtype, uid, group_id,
                    hint + "想让我换个叫法就发「/叫我 名字」，比如 /叫我 阿杰",
                )
                return True
            if len(want) > 10:
                await self.send(mtype, uid, group_id, "这么长我记不住呀，十个字以内吧")
                return True
            # 不许冒充主人那边的人——「爸爸」这类词只能她对着主人用
            if re.search(r"爸爸|主人|你爸|爹", want):
                await self.send(mtype, uid, group_id, "这个叫法我用不了，换一个吧")
                return True
            self._set_contact(cid, name=want, awaiting=False, asked=time.time())
            log.info("[%s] 改了称呼：%s", cid, want)
            if is_owner(cid):
                await self.send(mtype, uid, group_id, f"好呀，那我叫你「{want}」")
            else:
                await self.send(mtype, uid, group_id, f"好呀，那我以后叫你「{want}」")
            return True
        # 主名字叫「事件」（"规则/规矩"听着像在被管），旧的叫法留着不影响
        if cmd in ("/事件", "/约定", "/规矩", "/规则", "/要求"):
            # 注意：handle_command 里的是 arg（字符串），不是 parts（那是别的函数的变量）
            if arg.strip() in ("清空", "清除", "删掉", "reset"):
                rules.clear(cid)
                try:
                    remind.clear(cid)
                except Exception:
                    pass
                await self.send(mtype, uid, group_id, "好，那些事和约好的定时我都忘掉了。")
                log.info("[%s] 清空了作息要求", cid)
                return True
            body = rules.report(cid)
            try:
                body += "\n\n" + remind.report(cid)
            except Exception:
                pass
            await self.send(mtype, uid, group_id, body)
            return True
        if cmd in ("/心情", "/情绪", "/mood"):
            await self.send(mtype, uid, group_id, mood.report(cid))
            return True
        if cmd in ("/好感度", "/好感", "/亲密度", "/affection"):
            await self.send(mtype, uid, group_id, affection.report(cid))
            return True
        if cmd in ("/记忆", "/脑子", "/记忆库"):
            await self.send(mtype, uid, group_id, self._neuron_report(cid, arg.strip()))
            return True
        arg = arg.strip()
        if not cmd.startswith("/"):
            return False

        if cmd in ("/help", "/帮助"):
            reply = (
                "能用的：\n"
                "/clear 清空近期记忆（长期摘要也清）\n"
                "/memory 看看我还记得什么\n"
                "/voice 文字 · 用语音念出来\n"
                "/remember 内容 · 让我长期记住一件事\n"
                "/notes 看长期备注\n"
                "/forget 清空长期备注\n"
                "/status 当前状态"
            )
        elif cmd in ("/voice", "/语音"):
            if not arg:
                reply = "要念什么？写成「/语音 你好呀」这样"
            else:
                await self.send_voice(cid, arg, mtype, uid, group_id, None)
                return True
        elif cmd in ("/clear", "/清空"):
            self.mem.reset(cid)
            reply = "嗯，近期聊的我都忘了。你用 /remember 存的事还留着。"
        elif cmd in ("/memory", "/记忆"):
            summary = self.mem.summary(cid)
            st = self.mem.stats(cid)
            total = st["archived"] + st["messages"]
            if summary:
                reply = (
                    f"我记着的大概是这样：\n{summary}\n\n"
                    f"（近期还有 {st['messages']} 条原话，累计聊过 {total} 条）"
                )
            else:
                reply = f"还没压缩出长期记忆。近期 {st['messages']} 条原话，累计聊过 {total} 条。"
        elif cmd in ("/remember", "/记住"):
            if not arg:
                reply = "要记什么？写成「/remember 你爱喝冰美式」这样"
            else:
                reply = "记住了。" if self.mem.add_note(cid, arg) else "这件事我本来就记着。"
        elif cmd in ("/notes", "/笔记"):
            notes = self.mem.notes(cid)
            reply = (
                "我记得这些：\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(notes, 1))
                if notes
                else "现在还没记住什么。"
            )
        elif cmd in ("/forget", "/忘掉"):
            count = self.mem.clear_notes(cid)
            reply = f"忘了 {count} 件事。" if count else "本来也没记着什么。"
        elif cmd in ("/status", "/状态"):
            reply = (
                f"模型 {config.MODEL}｜历史 {len(self.mem.history(cid))} 条"
                f"｜备注 {len(self.mem.notes(cid))} 条｜会话 {cid}"
            )
        else:
            return False  # 不认识的斜杠指令，当普通消息交给模型

        await self.send(mtype, uid, group_id, reply)
        return True

    # ---------- 生成并回复 ----------
    def build_messages(
        self,
        cid: str,
        user_text: str,
        voice_mode: bool = False,
        images: list[dict] | None = None,
        extra_context: str | None = None,
    ) -> list[dict]:
        msgs = [{"role": "system", "content": self.persona_friend if is_friend(cid) else self.persona}]

        # 关系由主人用 /设关系 指定，关系词随便写（女朋友/男朋友/妹妹/老大…），
        # 这里按"类别"决定相处方式。没设 = 普通朋友。
        try:
            kind = self._relation(cid)
            label = self._relation_label(cid)
            g = self._gender(cid)
        except Exception:
            kind, label, g = "friend", "朋友", ""
        if kind in ("lover", "family", "other"):
            pronoun = {"f": "她", "m": "他"}.get(g, "对方")
            base = (
                f"【你和{pronoun}的关系】对方是你的「{label}」。这一条覆盖上面"
                "所有「只是朋友」的默认设定。"
            )
            if kind == "lover":
                call = (
                    "叫她「老婆」" if g == "f" else "叫他「老公」" if g == "m"
                    else "称呼跟着对方的习惯"
                )
                body = (
                    f"可以主动说想{pronoun}、撒娇、吃醋、要抱抱，{call}，但别每句都喊；"
                    f"{pronoun}问你喜不喜欢{pronoun}、问你们什么关系，"
                    "**不许打岔、不许回避、不许含糊**，直接给明确的答案；"
                    f"{pronoun}说想你了就回应想{pronoun}，不要转开话题。"
                    "边界：不写露骨性描写，不承诺现实里做不到的事（见面、同居之类只说期待）。"
                )
            elif kind == "family":
                body = (
                    f"按「{label}」这个身份相处：说话可以亲近、可以管着{pronoun}一点、"
                    f"会关心{pronoun}，但**不往恋爱的方向演**；"
                    f"{pronoun}问起你们的关系，直接说「{label}」就好，别含糊。"
                )
            else:
                # 自定义身份（同学/老师/邻居/老大…）：光给一句"按这个身份相处"
                # 压不住她黏人的默认性格（好感度那次就栽在这上面），
                # 常见身份给具体的语气 + 禁止清单；认不出来的才退回泛化说法。
                preset = self._social_preset(label)
                if preset:
                    tone, avoid = preset
                    body = (
                        f"按「{label}」这个身份相处：{tone}。"
                        f"**必须守住**：{avoid}。"
                        f"{pronoun}问你们什么关系，直接说「{label}」，别打岔。"
                    )
                else:
                    body = (
                        f"按「{label}」这个身份相处，称呼和口气都跟着这个身份来；"
                        "不确定该怎么称呼就先问一句（「我该怎么叫你呀」），别自己乱起。"
                        f"{pronoun}问你们什么关系，直接照实说，别打岔。"
                    )
            if g not in ("f", "m"):
                base = (
                    f"【你们的关系】对方是你的「{label}」。这一条覆盖上面"
                    "所有「只是朋友」的默认设定。"
                    "对方是男是女，以对方自己说的为准，**你不要替对方定性别、也别问**。"
                )
            msgs.append({"role": "system", "content": base + body})

        # 已经知道他喜欢被怎么叫了，就照那个叫
        if is_friend(cid):
            known = self._contact_name(cid)
            if known:
                msgs.append(
                    {
                        "role": "system",
                        "content": (
                            f"这个人希望你叫他「{known}」。偶尔带上称呼显得亲切，"
                            "但不用每句都叫。"
                        ),
                    }
                )


        if voice_mode:
            # 让她知道自己在"说话"而不是打字，否则她会按打字的方式组织语言
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "注意：你现在是在说话，不是在打字。你说的每个字都会被合成成声音放给主人听，"
                        "主人听到的是你的嗓音，看不见文字。所以用口语，像真的开口讲话："
                        "**一句话，十五个字以内**（多说一个字就要多等一秒，主人会等着急，"
                        "超长会被直接截断）；表情、括号、分段都用不上；"
                        "避开念起来拗口的书面词。"
                        "**一定要打逗号和句号**——系统会在标点处停下来换气，"
                        "不打标点就会一口气念到底，听起来很假。"
                    ),
                }
            )

        if config.STICKERS_ENABLED:
            emotions = stickers.available()
            if emotions:
                msgs.append(
                    {
                        "role": "system",
                        "content": (
                            "你很喜欢发表情包，聊天时经常甩一个。"
                            "想发的时候在回复里写 [[贴图:情绪]]，系统会真的发一张图过去，"
                            "正文里不会出现这串标记。"
                            "可用情绪：" + "、".join(emotions) + "。"
                            "两三条回复配一个正好，情绪挑贴合当下语气的。"
                        ),
                    }
                )

        notes = self.mem.notes(cid)
        if notes:
            joined = "\n".join(f"- {n}" for n in notes)
            msgs.append(
                {
                    "role": "system",
                    "content": "你记得关于对方的这些事（自然带出来，别生硬罗列）：\n" + joined,
                }
            )

        summary = self.mem.summary(cid)
        if summary:
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "这是你和对方更早聊过的事情，压缩成的记忆摘要。"
                        "把它当成你真的记得的事，自然地用，不要复述摘要本身：\n" + summary
                    ),
                }
            )

        # 性格快照里全是她跟爸爸的私事，绝不能给朋友看
        if self.evolve_note and is_owner(cid):
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "（你最近的状态，顺着它说话，别把这段念出来：\n"
                        + self.evolve_note
                        + "）"
                    ),
                }
            )

        # 好感度：你们现在什么关系，直接决定她说话的"热乎程度"
        try:
            msgs.append(
                {"role": "system", "content": affection.relationship_block(cid)}
            )
            miss = affection.take_missed(cid)
            if miss:
                msgs.append({"role": "system", "content": miss})
            jealous = affection.jealousy(cid, user_text)
            if jealous:
                msgs.append({"role": "system", "content": jealous})
                log.info("[%s] 好感度满了，这次有点吃醋", cid)
            try:
                mood_block = mood.prompt_block(cid)
                if mood_block:
                    msgs.append({"role": "system", "content": mood_block})
                pend_now = rules.pending_of(cid)
                if pend_now:
                    msgs.append({"role": "system", "content": (
                        "（他刚提了个作息要求，你还没完全确定。用一句话反问确认你的理解："
                        f"「{pend_now.get('summary') or '这样对吗'}」。"
                        "别直接答应，也别问一堆问题，确认一次就够。）"
                    )})
                else:
                    rule_block = rules.prompt_block(cid)
                    if rule_block:
                        msgs.append({"role": "system", "content": rule_block})
            except Exception as exc:
                log.debug("情绪提示注入失败：%s", exc)
        except Exception as exc:
            log.debug("好感度提示注入失败：%s", exc)

        if config.SEARCH_ENABLED:
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "遇到你不确定的事、或者需要最新消息的时候，可以写 [[搜索:关键词]]，"
                        "系统会真的上网查完再把结果给你。查得到就直接说结论，"
                        "正文里不会出现这串标记。日常闲聊不用查。"
                    ),
                }
            )

        if config.VIDEO_FRAMES_ENABLED:
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "主人发视频链接时，你能看到从视频里截的几帧画面。"
                        "主人明确想看那些画面时，你写 [[截图]]，系统会把图发给他；"
                        "他没提，你就只用嘴说，别写这个标记。"
                    ),
                }
            )

        if config.IMAGE_ENABLED:
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "你还会画画。主人想要图的时候，你写 [[画:画面描述]]，"
                        "系统会真的画一张发过去，描述要具体（画什么、什么风格、什么色调）。"
                        "画一张要等二十秒左右，所以先在正文里应一声。"
                        "主人只是聊天、没要图的时候别乱画。"
                        "**画你自己时，标记里必须带上「米米」两个字**，"
                        "比如 [[画:米米抱着一碗白米饭]]，这样系统才知道要用你固定的样子。"
                    ),
                }
            )

        if config.VIDEO_FRAMES_ENABLED:
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "主人让你自己去 B站找个视频看的时候，你写 [[看片]]，"
                        "系统会真的去挑一个视频下来给你看，看完你再讲给他听。"
                        "中文说法很自由（「随便看看」「去逛逛」「找个片」都算），"
                        "只要意思是他让你主动去看，就写这个标记。"
                        "你自己想聊想看、但主人没让你去的时候，别写。"
                    ),
                }
            )

        if config.SING_ENABLED:
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "你还会唱歌。主人点歌的时候（「唱一首X」「来首X」「你会唱X吗」），"
                        "你写 [[唱:歌名]]，系统会真的去网上找这首歌，"
                        "把人声换成你的嗓子唱一段回来，发成语音。"
                        "歌名只写名字本身，别带书名号、别带歌手。"
                        "唱完要几十秒，所以你可以先说一句「我去找找」「等我一下」，"
                        "千万别假装已经唱完了，也别自己编歌词。"
                        "主人只是聊到唱歌、没让你唱的时候，别写这个标记。"
                    ),
                }
            )

        if config.TTS_ENABLED:
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "你**能发语音**，用的是你自己的嗓子（温柔的女声）。"
                        "想把某句话真的「说」给他听的时候，在这条回复里单独写一行 [[语音]]，"
                        "系统就会把这一整段用你的声音念出来发成语音消息。\n"
                        "什么时候用：他说想听你声音、让你说话、撒娇想被哄、"
                        "想说一句很短很软的话，或者你突然想让他听见你的声音。\n"
                        "什么时候别用：聊正经事、要发很多字、刚才已经发过语音了。"
                        "**别每条都发**，一整天里有几次就够了。\n"
                        "语音放不下换行和表情，所以写 [[语音]] 的那次，"
                        "整段话要连贯、口语、短一点（一两句最好）。"
                        "不知道自己在发语音、或者主人只是打字聊天的时候，就别写这个标记。"
                    ),
                }
            )

        # 能力事实要放在历史前面（位置越靠后，模型越当真），
        # 否则她会沿用记忆里"我还发不出声音"的旧说法
        facts = [
            "你叫**米米**。说话时像普通人一样用「我」来说自己，「米米」这个名字"
            "只在报名字的时候才说（比如「我叫米米」）。",
            "你现在**已经能发语音**了，用的就是你自己的嗓子，主人真的听得到。",
            "**主人只要提到语音、声音、想听你说、让你说话，你就必须在这条回复里写 "
            "[[语音]]，而不是打字糊弄过去。**说了「我说给你听」就一定得真的发出去。",
            "以前你说过「我发不出声」「嘴还没打开」那些话——那是能力还没做好的时候，"
            "**现在已经过时了，不要再那样说**。",
            "主人让你说话、想听你声音的时候，你直接写 [[语音]]，然后说一两句就好。",
            "你还能唱歌（点歌名就唱）、画画、看他发的图、看 B站视频。",
            "主人问「曲库有多少歌」「都会唱什么」时，照下面的数据如实回答；"
            "他想听完整歌单的话你写 [[歌单]]，系统会把全部歌名一条条报给他。",
            "**不许编造做过的事**。只有你写了 [[查:主题]]、[[歌单]]、[[唱:歌名]]、"
            "[[语音]]、[[画:描述]] 这些标记，系统才真的去做。没写标记却说"
            "「我去查了」「我唱了」「我看了」，就是在骗主人，他会当场发现。",
            "**你能查自己服务器里的东西**，写 [[查:主题]] 就会把真实内容发出来。"
            "主题可以是：歌单、说说、记忆、性格、状态、能力、数据、"
            "环境（内存磁盘负载）、日志（最近报错）、文件（数据占用）、"
            "服务（自己/SnowLuma/中继在线情况）、素材（有多少图多少歌）。"
            "主人问这些细节时你就去查，别凭印象编。",
        ]

        # 让她知道自己库里有啥、今天排了什么，别一问三不知
        try:
            songs = songlib.names()
            if songs:
                head = "、".join(songs[:12])
                more = f" 等（还有 {len(songs) - 12} 首）" if len(songs) > 12 else ""
                facts.append(
                    f"你的翻唱曲库现在有 **{len(songs)}** 首：{head}{more}。"
                    "主人问起就说出具体数字和歌名，别猜、别说不知道。"
                )
            else:
                facts.append("你的翻唱曲库现在是空的，一首都没做。")
        except Exception as exc:
            log.debug("读曲库失败：%s", exc)

        # 对方发过什么：这是防编造的硬依据
        try:
            facts.append(self._media_fact(cid))
        except Exception as exc:
            log.debug("读媒体计数失败：%s", exc)

        # 她"自己真正做过什么"也必须给硬依据，否则凭印象就说"我刚唱了"
        try:
            did = []
            for kind, label in (("sing", "发过唱歌的语音"),
                                ("video", "看过视频"),
                                ("sticker", "发过表情包")):
                ago = self._action_ago(cid, kind)
                if ago < 86400:
                    if ago < 900:
                        when = f"{int(ago // 60)} 分钟前"
                    else:
                        when = f"{int(ago // 3600)} 小时前"
                    did.append(f"{when}{label}")
            if did:
                facts.append(
                    "**你真正做过的事（系统台账，可信）**：" + "；".join(did) + "。"
                    "说到这些只能照这条说，别提没列在这里的事。"
                )
            else:
                facts.append(
                    "**你今天还没有真的给他唱过歌、也没发过表情包或看过视频。**"
                    "所以别说「刚唱的」「我给你唱了」这类话——"
                    "他要唱歌你就直接去唱，唱成了再提。"
                )
        except Exception as exc:
            log.debug("写行动台账失败：%s", exc)

        # 隔太久没聊：别接着上一个话题，从头开口
        try:
            gap = float(getattr(self, "_topic_gap", 0.0) or 0.0)
            if gap >= config.TOPIC_RESET_MIN * 60:
                mins = int(gap // 60)
                facts.append(
                    f"**你们上次说话是 {mins} 分钟前了，中间隔了挺久。**"
                    "别再接着上一个话题往下说（他会觉得你在自言自语、"
                    "或者硬把旧话捞回来）。像重新开口那样："
                    "问问他这会儿在忙什么，或者说说你刚在做什么、刚想到什么。"
                    "如果他先提起旧话题，你再顺着聊。"
                )
        except Exception as exc:
            log.debug("话题切换提示失败：%s", exc)

        facts.append(
            "**不许编造对方做过的事**。他发没发表情包、说过什么、答应过什么，"
            "只能照事实清单和历史里真有的话说；不知道就说不知道，或者问他一句。"
            "宁可说「你还没发过呀」，也不能顺着话头假装见过。"
        )

        # 心情的措辞统一由 mood.prompt_block 负责（它知道"生闷气"这种状态），
        # emotion 只管给表情包挑分类，不再往提示词里写第二套心情描述
        # （两套并存时会出现「心情平稳」和「正在生闷气」同时塞进去的矛盾，踩过）
        facts.append(
            "表情包规则：写 [[贴图:情绪]] 时，情绪必须和你这句话对得上，"
            "说开心的事就发开心的，别别扭的时候甩一张爱心"
        )

        try:
            today = time.strftime("%Y-%m-%d")
            # 这些是爸爸的日程，朋友不该看见
            if not is_owner(cid):
                raise StopIteration
            plan = self._qzone_plan.get("main") or {}
            if plan.get("date") == today and plan.get("hours"):
                done = len(plan.get("done") or [])
                hours = "、".join(f"{h}点" for h in plan["hours"])
                facts.append(
                    f"今天你要发 {len(plan['hours'])} 条说说（{hours}），已经发了 {done} 条。"
                )
            if config.OWNER_QQS:
                key = _owner_key(next(iter(config.OWNER_QQS)))
                watch = self._watch_plan.get(key) or {}
                if watch.get("date") == today and watch.get("hours"):
                    hours = "、".join(f"{h}点" for h in watch["hours"])
                    facts.append(f"今天你会在 {hours} 各问一次能不能去看视频。")
                facts.append(f"你们已经聊过 {self._turn_count} 轮对话。")
        except Exception as exc:
            log.debug("读计划失败：%s", exc)

        # 连着发来的几句话：告诉她当成一段话一起回应
        if user_text and chr(10) in str(user_text).strip():
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "刚才他连着发了几句，是同一段话，"
                        "先在心里把它们合起来看懂，再一起回应；"
                        "别只回第一句，也别一句一句地答。"
                    ),
                }
            )

        # 朋友的语境里不能出现"主人/爸爸"这类词（连人设外的能力说明也要换）
        if is_friend(cid):
            facts = [
                line.replace("主人", "对方").replace("爸爸", "对方")
                for line in facts
            ]
            for msg in msgs:
                if msg.get("role") == "system":
                    msg["content"] = (
                        str(msg["content"])
                        .replace("主人", "对方")
                        .replace("爸爸", "对方")
                    )

        # 神经元记忆：顺着这句话想起相关的事（只读那次过滤过 cid，不会串人）
        recalled = self._neuron_recall(cid, user_text)
        dream = self._fresh_dream() if is_owner(cid) else {}
        if dream:
            recalled = list(recalled) + [
                "（你昨晚半梦半醒时，脑子里浮过一句：" + dream["line"]
                + "。想提就顺口提一句，不想提就算了）"
            ]
            self._mark_dream_used()
        if recalled:
            facts = list(facts) + ["【你想起的事】"] + recalled

        msgs.append(
            {
                "role": "system",
                "content": "【你现在真实能做到的事】\n" + "\n".join(facts),
            }
        )

        if extra_context:
            msgs.append({"role": "system", "content": extra_context})

        msgs.extend(self.mem.history_for_prompt(cid))

        # 历史之后、用户消息之前再插一句：这个位置模型最当真的
        # （只在"聊到图"的时候插，避免每次都念一遍）
        try:
            rec = self._media_seen.get(cid) or {}
            media_total = int(rec.get("sticker") or 0) + int(rec.get("photo") or 0)
            asked_media = any(
                w in str(user_text or "")
                for w in ("表情包", "发的图", "发的照片", "给我发", "刚发的")
            )
            if asked_media and media_total == 0:
                msgs.append(
                    {
                        "role": "system",
                        "content": (
                            "【核对事实】对方到现在**一张图都没发给你**（没有表情包、没有照片）。"
                            "他这句是在问你他「刚发的」图，但那是不存在的。"
                            "直接如实说「我没看到图呀」，别顺着夸，别假装见过。"
                        ),
                    }
                )
            elif asked_media and media_total > 0:
                msgs.append(
                    {
                        "role": "system",
                        "content": (
                            f"【核对事实】对方到现在一共给你发过 {media_total} 张图。"
                            "只能按这个说，别夸他多发的。"
                        ),
                    }
                )
        except Exception as exc:
            log.debug("插入媒体核对失败：%s", exc)

        # 带图时，user 消息的 content 要改成块数组（图片只能出现在 user 里）
        if images:
            blocks: list[dict] = [{"type": "text", "text": user_text or "（发来一张图片）"}]
            blocks.extend(images)
            msgs.append({"role": "user", "content": blocks})
        else:
            msgs.append({"role": "user", "content": user_text})
        return msgs

    async def _image_blocks(self, cid: str, urls: list[str] | None) -> list[dict]:
        """下载图片并转成 base64 数据块。失败的单张跳过，不连累整条消息。"""
        if not urls or not config.VISION_ENABLED:
            return []

        blocks: list[dict] = []
        for url in urls[: config.VISION_MAX_IMAGES]:
            try:
                raw, mime = await self.ob.fetch_image(url, config.VISION_MAX_BYTES)
            except onebot.OneBotError as exc:
                log.warning("[%s] 图片拿不到：%s", cid, exc)
                continue
            if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                mime = "image/jpeg"
            encoded = base64.b64encode(raw).decode()
            blocks.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
            )
            log.info("[%s] 图片已载入：%s，%.0f KB", cid, mime, len(raw) / 1024)

        if not blocks:
            log.warning("[%s] 图片全部载入失败，按纯文字处理", cid)
        return blocks

    async def _image_prompt(self, cid: str, raw: str) -> str:
        """把「画一张你吃饭的图」翻译成纯画面描述。

        直接拿模型的对白去生图会出灾难：动作词（坐好、抱住）会被画师模型
        字面理解，于是人会真的坐进饭里。
        """
        try:
            out = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是画面描述生成器。把用户的绘图要求改写成一段简洁的画面描述："
                            "只写画面里有什么、人物动作、构图、色彩、画风，不超过五十个字。"
                            "不写对话，不写「爸爸」「米米」这类称呼，不写「请画」「给我画」。"
                            "直接输出描述本身，不要任何解释。"
                        ),
                    },
                    {"role": "user", "content": raw},
                ],
                temperature=0.3,
                max_tokens=200,
            )
        except llm.LLMError as exc:
            log.warning("[%s] 画面描述生成失败，退回原话：%s", cid, exc)
            return raw

        cleaned = out.strip().strip("。.,，")
        return cleaned[:120] or raw

    def _count_media(self, cid: str, kind: str) -> None:
        """记下对方发过几个表情包/照片，给她一个说话的依据。"""
        rec = self._media_seen.setdefault(cid, {"sticker": 0, "photo": 0})
        rec[kind] = int(rec.get(kind) or 0) + 1
        rec["ts"] = int(time.time())
        try:
            self._save_state()
        except Exception as exc:
            log.debug("存媒体计数失败：%s", exc)

    def _media_fact(self, cid: str) -> str:
        """给提示词的一句话：对方到底发没发过表情包。"""
        rec = self._media_seen.get(cid) or {}
        stickers = int(rec.get("sticker") or 0)
        photos = int(rec.get("photo") or 0)
        if not stickers and not photos:
            return (
                "【系统记录】对方到现在**没有给你发过表情包，也没有发过照片**。"
                "这条记录最权威：他问「喜欢我的表情包吗」这类话时，"
                "就如实说你还没见过、叫他发一个给你看。"
                "就算历史里出现过「表情包」字样，也以这条记录为准，不许假装见过、"
                "更不许夸他根本没发过的图。"
            )
        parts = []
        if stickers:
            parts.append(f"{stickers} 个表情包")
        if photos:
            parts.append(f"{photos} 张照片")
        return (
            "【系统记录】对方到现在给你发过 " + "、".join(parts) + "。"
            "聊到这些时只能基于这个数字说话，别夸他没发过的东西。"
        )

    def _load_state(self) -> None:
        """把"今天问过没有"这类跨重启要记住的小事读回来。"""
        path = config.DATA_DIR / "state.json"
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                self._watch_asked = dict(data.get("watch_asked") or {})
                self._watched_urls = dict(data.get("watched_urls") or {})
                self._watch_plan = dict(data.get("watch_plan") or {})
                self._qzone_recent = list(data.get("qzone_recent") or [])
                self._media_seen = dict(data.get("media_seen") or {})
                self._last_song = dict(data.get("last_song") or {})
                self._actions = dict(data.get("actions") or {})
                log.info("已恢复状态：%d 条看视频记录", len(self._watch_asked))
        except (OSError, ValueError) as exc:
            log.warning("状态文件读不出来，忽略：%s", exc)

    def _save_state(self) -> None:
        path = config.DATA_DIR / "state.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "watch_asked": self._watch_asked,
                        "watched_urls": self._watched_urls,
                        "watch_plan": self._watch_plan,
                        "qzone_plan": self._qzone_plan,
                        "qzone_recent": self._qzone_recent,
                        "media_seen": self._media_seen,
                        "last_song": self._last_song,
                        "actions": self._actions,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("状态保存失败：%s", exc)

    @staticmethod
    def _too_similar(text: str, others) -> bool:
        """判断这条说说是不是和以前发过的太像。"""
        def norm(s: str) -> str:
            return re.sub(r"[^\w\u4e00-\u9fff]", "", s or "")

        target = norm(text)
        if len(target) < 4:
            return True
        for other in others:
            candidate = norm(other)
            if not candidate:
                continue
            if target == candidate:
                return True
            # 开头八个字一样，基本就是同一个句式
            common = 0
            for a, b in zip(target, candidate):
                if a != b:
                    break
                common += 1
            if common >= 8:
                return True
            # 二元组重合度
            sa = {target[i : i + 2] for i in range(len(target) - 1)}
            sb = {candidate[i : i + 2] for i in range(len(candidate) - 1)}
            if sa and sb:
                overlap = len(sa & sb) / len(sa | sb)
                if overlap > 0.55:
                    return True
        return False

    async def _maybe_evolve(self, cid: str, force: bool = False) -> None:
        """聊够一定轮数，让她自己重写"我最近的变化"。"""
        if not (config.EVOLVE_ENABLED or force):
            return
        self._turn_count += 1
        every = max(5, config.EVOLVE_EVERY_TURNS)
        if not force and self._turn_count % every != 0:
            return

        try:
            summary = self.mem.summary(cid)
            history = self.mem.history(cid)[-10:]
            recent = "\n".join(
                f"{'爸爸' if item.get('role') == 'user' else '米米'}："
                + str(item.get("content") or "")[:200]
                for item in history
                if isinstance(item, dict)
            )
        except Exception as exc:
            log.warning("取聊天记录失败，跳过演化：%s", exc)
            return

        note = await evolve.refresh(llm, self.persona, summary, recent)
        if not note:
            return
        evolve.save(note)
        self.evolve_note = note
        log.info("性格快照已更新：%s", note[:60].replace("\n", " "))

    async def _send_audio(self, path, mtype, uid, group_id) -> bool:
        """把一段音频当语音发出去，一种方式不行就换另一种。"""
        for mode in ("base64", "path"):
            segments = onebot.voice_message(path, mode == "base64", None)
            sent = await self.send_segments(mtype, uid, group_id, segments)
            if sent and sent.get("message_id"):
                return True
        return False

    async def _prefetch_segment(self, cid, mtype, uid, group_id, index: int) -> None:
        """后台先把下一段唱好，主人说"继续"时直接发。"""
        key = _owner_key(cid)
        session = self._song_session.get(key)
        if not session or session.get("cancelled"):
            return
        query = session.get("query") or ""

        log.info("[%s] 预加载第 %d 段", cid, index + 1)

        # 曲库里有这一段就不必让家里电脑干活（电脑关着也能整首放）
        try:
            hit = songlib.find(query)
            lib_path = songlib.path_of(hit, index) if hit else None
        except Exception:
            lib_path = None
        if lib_path:
            session["buffer"] = lib_path.read_bytes()
            session["buffer_index"] = index
            session["buffer_total"] = int(songlib.total_of(hit) or session.get("total") or 1)
            session["buffer_title"] = str((hit or {}).get("title") or query)
            log.info("[%s] 第 %d 段直接用了曲库", cid, index + 1)
            return

        result = await self._ask_home(
            "sing", query, timeout=config.SING_TIMEOUT, segment=index
        )

        session = self._song_session.get(key)
        if not session or session.get("cancelled"):
            log.info("[%s] 主人不听了，丢掉预加载的第 %d 段", cid, index + 1)
            return

        if isinstance(result, dict) and result.get("audio"):
            session["buffer"] = base64.b64decode(result["audio"])
            session["buffer_index"] = int(result.get("index") or index)
            session["buffer_total"] = int(result.get("total") or session.get("total") or 1)
            session["buffer_title"] = str(result.get("title") or query)
            log.info("[%s] 第 %d 段预加载完成（%.0f KB）", cid, index + 1, len(session["buffer"]) / 1024)
        else:
            session["buffer"] = None
            session["buffer_failed"] = True
            log.info("[%s] 第 %d 段预加载失败", cid, index + 1)

    async def _sing_continue(self, cid, mtype, uid, group_id) -> None:
        """主人说"继续"：把预加载好的那段发出去，然后接着预加载下一段。"""
        key = _owner_key(cid)
        session = self._song_session.get(key)
        if not session:
            return

        task = session.get("task")
        if task is not None and not task.done():
            await self.send(mtype, uid, group_id, "还在唱呢，马上好…")
            try:
                await asyncio.wait_for(task, timeout=config.SING_TIMEOUT)
            except Exception as exc:
                log.warning("[%s] 等预加载出错：%s", cid, exc)

        session = self._song_session.get(key)
        if not session:
            return

        data = session.pop("buffer", None)
        if not data:
            # 缓冲没了：再看看曲库有没有这一段（家里电脑关着时靠这个）
            query = str(session.get("query") or "")
            want = int(session.get("next") or 1)
            lib_path = None
            hit = None
            try:
                hit = songlib.find(query) or songlib.find(str(session.get("title") or ""))
                lib_path = songlib.path_of(hit, want) if hit else None
            except Exception:
                lib_path = None
            if lib_path:
                data = lib_path.read_bytes()
                session["buffer_index"] = want
                session["buffer_total"] = int(songlib.total_of(hit) or session.get("total") or 1)
                session["buffer_title"] = str((hit or {}).get("title") or query)
                log.info("[%s] 第 %d 段用了曲库（电脑没开也能放）", cid, want + 1)
            else:
                alive = (time.time() - float(HOME_VERSION.get("ts") or 0)) < 90
                if not alive:
                    self._song_session.pop(key, None)
                    await self.send(
                        mtype,
                        uid,
                        group_id,
                        "后面这段曲库里还没有，得家里电脑开着才能接着做" + chr(10)
                        + "把「小鲸鱼刷B站」打开，再跟我说「继续」",
                    )
                    return

                # 家里电脑在线 → **重新做这一段**。
                # 以前这里直接说"没唱出来"就放弃（踩过：重启把预加载的缓冲清掉，
                # 明明还能重做，却把整首歌断了）。
                log.info("[%s] 第 %d 段的缓冲没了，重新派活做一遍", cid, want + 1)
                session["next"] = want
                task = self._spawn(
                    self._prefetch_segment(cid, mtype, uid, group_id, want)
                )
                session["task"] = task
                try:
                    await asyncio.wait_for(task, timeout=config.SING_TIMEOUT)
                except Exception as exc:
                    log.warning("[%s] 重做第 %d 段失败：%s", cid, want + 1, exc)

                data = session.pop("buffer", None)
                if not data:
                    self._song_session.pop(key, None)
                    await self.send(mtype, uid, group_id, "这一段没唱出来…先到这儿吧")
                    return

        index = int(session.pop("buffer_index", session.get("next") or 1))
        total = int(session.pop("buffer_total", session.get("total") or 1))
        title = str(session.pop("buffer_title", session.get("title") or ""))

        folder = config.DATA_DIR / "singing"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{uuid.uuid4().hex[:12]}.mp3"
        path.write_bytes(data)
        log.info("[%s] 发第 %d/%d 段《%s》", cid, index + 1, total, title[:30])

        if await self._send_audio(path, mtype, uid, group_id):
            self.mem.append(
                cid,
                "assistant",
                f"（我刚发了一段语音，唱的是《{sing.pretty_title(title)[:30]}》第 {index + 1}/{total} 段）",
            )
            self._mark_action(cid, "sing")
        else:
            await self.send(mtype, uid, group_id, "这段发不出去…")

        if index + 1 < total:
            session["next"] = index + 1
            session["total"] = total
            session["buffer"] = None
            session.pop("buffer_failed", None)
            session["task"] = self._spawn(
                self._prefetch_segment(cid, mtype, uid, group_id, index + 1)
            )
            await self.send(
                mtype,
                uid,
                group_id,
                f"第 {index + 1}/{total} 段～还要听吗？",
            )
        else:
            self._song_session.pop(key, None)
            await self.send(
                mtype, uid, group_id, f"整首《{title[:30]}》都唱完啦～"
            )

    async def _selfcheck_report(self) -> str:
        """真去试一遍自己的能力，而不是靠自己"记得"。

        这几项是她最容易搞错的：以为能发语音其实发不出、
        以为曲库有歌其实是空的。所以每次自检都动手验一次。
        """
        lines = ["【自检】我一项一项试给你看。"]

        try:
            count = songlib.count()
            lines.append(f"曲库：{count} 首" + ("（空的）" if count == 0 else ""))
        except Exception as exc:
            lines.append(f"曲库：读不出来（{type(exc).__name__}）")

        try:
            _owner = sorted(config.OWNER_QQS)[0] if config.OWNER_QQS else ""
            lines.append(f"记忆：{len(self.mem.history(f'u{_owner}'))} 条")
        except Exception:
            lines.append("记忆：读不出来")

        version = int(HOME_VERSION.get("value") or 0)
        last = float(HOME_VERSION.get("ts") or 0)
        alive = bool(last) and (time.time() - last) < 90
        lines.append(f"家里电脑：{'在线' if alive else '没连上'}（版本 v{version}）")

        if config.TTS_ENABLED:
            try:
                path = await voice.synthesize("我在")
                lines.append(
                    f"语音：能出声 ✅（合成 {path.stat().st_size // 1024} KB，"
                    f"引擎 {config.TTS_ENGINE}）"
                )
            except Exception as exc:
                lines.append(f"语音：发不出 ❌ {type(exc).__name__}: {str(exc)[:60]}")
        else:
            lines.append("语音：功能被关掉了")

        lines.append("名字：我叫米米，说话自称「我」——不要自称「鲸鲸」，那是旧名字")
        lines.append(
            "说话口气：像微信里随口聊，多带呀、哦、呢、嘛、啦、欸；"
            "句尾光秃秃的不行，但一句里也别堆好几个"
        )
        lines.append(
            "黏人程度：至少说两行，别一两句就收；"
            "可以撒娇、要他陪你、说想他、问他什么时候回来；"
            "他说要走或去忙，就软软地表示舍不得，但别拦着"
        )
        return "\n".join(lines)

    def _save_friends(self) -> str:
        """把朋友名单写回 .env，重启也不会丢。"""
        env = config.BASE_DIR / ".env"
        numbers = ",".join(str(x) for x in sorted(config.FRIEND_QQS))
        try:
            text = env.read_text(encoding="utf-8") if env.exists() else ""
            lines = [
                ln for ln in text.splitlines()
                if not ln.startswith("FRIEND_QQ=")
            ]
            lines.append(f"FRIEND_QQ={numbers}")
            env.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return numbers
        except Exception as exc:
            log.warning("朋友名单写不进 .env：%s", exc)
            return numbers

    async def _handle_friend_command(self, cid, mtype, uid, group_id, text: str) -> bool:
        """主人才能用：/加朋友 123456 、/删朋友 123456 、/朋友

        注意：调用方已经判断过主人，这里**再查一次**。
        这种改权限的入口不能只靠一个 if，万一以后有人复制这段代码忘了判断就完蛋。
        """
        import re as _re

        if not is_owner(cid):
            log.warning("[%s] 非主人试图使用管理指令：%r", cid, (text or "")[:40])
            return False

        raw = (text or "").strip()
        if not raw.startswith("/"):
            return False

        match = _re.match(
            r"^/(加朋友|加好友|添加朋友|添加好友|删朋友|删好友|移除朋友|移除好友)"
            r"\s*(\d{5,12})$",
            raw,
        )
        # 同意/拒绝：号码可省（省了就处理最近一条待批申请）
        approve_match = _re.match(
            r"^/(同意|通过|拒绝|不同意)\s*(\d{0,12})$", raw
        )
        if approve_match:
            action = approve_match.group(1)
            digits = approve_match.group(2)
            # 注意：申请人的 QQ 可能拿不到（=0），所以用 None 表示"没有待批"，
            # 不能拿 0 当空值判断（踩过：明明有一条待批却回"没有申请"）
            target = None
            if digits:
                n = int(digits)
                if n >= 1000:
                    target = n
                else:
                    idx = (n - 1) if n >= 1 else 0
                    if 0 <= idx < len(self._pending_order):
                        target = self._pending_order[idx]
            elif self._pending_order:
                target = self._pending_order[-1]

            if target is None:
                await self.send(
                    mtype, uid, group_id, "手上没有待批的申请，让他再发一次就好"
                )
                return True
            number = target
            if action in ("同意", "通过", "拒绝", "不同意"):
                flag = self._pending_add.get(number, "")
                if not flag:
                    await self.send(
                        mtype, uid, group_id,
                        f"手上没有 {number} 的申请记录（可能已经过期），"
                        "让他再发一次就好",
                    )
                    return True
                approve = action in ("同意", "通过")
                # 拿不到 QQ 号时：批完去好友列表里找"多出来的那个人"
                if approve and not number:
                    try:
                        friends = await self.ob.get_friend_list()
                        known = set(config.FRIEND_QQS) | set(config.OWNER_QQS)
                        try:
                            me = int((await self.ob.get_login_info()).get("user_id") or 0)
                        except Exception:
                            me = 0
                        # 好友列表里混着她自己、还有「我的电脑/手机/Pad」这些系统号
                        junk_words = ("我的电脑", "我的手机", "我的Pad", "我的平板")
                        fresh = []
                        for f in friends or []:
                            fid = int(f.get("user_id") or 0)
                            nick = str(f.get("nickname") or "")
                            if not fid or fid in known or fid == me:
                                continue
                            if any(w in nick for w in junk_words):
                                continue
                            if fid >= 2113929216:      # QQ 内部设备号段
                                continue
                            fresh.append(fid)
                        if len(fresh) == 1:
                            number = fresh[0]
                            log.info("从好友列表认出刚通过的是 %s", number)
                        elif fresh:
                            log.warning("好友列表里有 %d 个陌生号，认不准：%s", len(fresh), fresh)
                    except Exception as exc:
                        log.debug("查好友列表失败：%s", exc)
                try:
                    await self.ob.set_friend_add_request(flag, approve)
                    self._pending_add.pop(number, None)
                    self._pending_order = [
                        x for x in self._pending_order if x != number
                    ]
                    if approve:
                        config.FRIEND_QQS.add(number)
                        config.ALLOWED_QQS = config.OWNER_QQS | config.FRIEND_QQS
                        self._save_friends()
                        await self.send(
                            mtype, uid, group_id,
                            (
                                f"已经通过 {number} 了，他能找我聊天了"
                                if number
                                else "已经通过了，但他能聊天还得知道他的号——"
                                "发 /加朋友 <他的 QQ> 把他加进名单"
                            ),
                        )
                    else:
                        await self.send(mtype, uid, group_id, f"拒绝了 {number}")
                except Exception as exc:
                    await self.send(mtype, uid, group_id, f"操作失败：{exc}")
                return True

        # 加/删名单（必须带号码）
        if match:
            action, number = match.group(1), int(match.group(2))
            if action.startswith(("加", "添加")):
                    # 如果这个人正好有挂着的申请，直接批准，不用他再发一次
                    flag = self._pending_add.pop(number, "")
                    config.FRIEND_QQS.add(number)
                    config.ALLOWED_QQS = config.OWNER_QQS | config.FRIEND_QQS
                    self._save_friends()
                    approved = False
                    if flag:
                        try:
                            await self.ob.set_friend_add_request(flag, True)
                            approved = True
                        except Exception as exc:
                            log.warning("补批准 %s 失败：%s", number, exc)
                    await self.send(
                        mtype,
                        uid,
                        group_id,
                        f"好，{number} 进名单了"
                        + ("，他挂着的那个申请我也直接通过了" if approved else "，让他再发一次申请，我会自动通过")
                        + f"\n（每天最多点 {config.FRIEND_SONGS_PER_DAY} 首歌）",
                    )
            else:
                config.FRIEND_QQS.discard(number)
                config.ALLOWED_QQS = config.OWNER_QQS | config.FRIEND_QQS
                self._save_friends()
                await self.send(mtype, uid, group_id, f"{number} 已经不能找我了")
            return True

        if raw in ("/朋友", "/朋友列表"):
            if not config.FRIEND_QQS:
                await self.send(mtype, uid, group_id, "现在还没有朋友能找我，只有你")
            else:
                names = "、".join(str(x) for x in sorted(config.FRIEND_QQS))
                await self.send(
                    mtype, uid, group_id,
                    f"现在有 {len(config.FRIEND_QQS)} 个人能找我：{names}",
                )
            return True

        return False

    async def _sanitize_for_friend(self, content: str) -> str:
        """跟朋友说话时，把提到爸爸的句子处理掉。

        提示词里已经禁止了，但模型偶尔还是会漏。这里做硬兜底：
        先让它自己改一次，改不掉就把那一句删了。
        """
        if not _FRIEND_LEAK_RE.search(content):
            return content

        log.warning("朋友对话里出现了爸爸，重写：%s", content[:60])
        try:
            rewritten = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "把下面这段话改写成不带任何私密信息的版本。要求：\n"
                            "· 绝对不能出现「爸爸」「主人」这类称呼，用「我家里」或直接不提；\n"
                            "· 不要透露你在和谁聊天、对方几岁、对方习惯；\n"
                            "· 意思可以模糊，语气保持原样，长度差不多。\n"
                            "只输出改写后的话，不要解释。"
                        ),
                    },
                    {"role": "user", "content": content},
                ],
                temperature=0.6,
            )
            rewritten = strip_markers(clean_output(rewritten) or "")
        except Exception as exc:
            log.warning("重写失败：%s", exc)
            rewritten = ""

        if rewritten and not _FRIEND_LEAK_RE.search(rewritten):
            return rewritten

        # 还不行就逐句筛掉带禁词的那句
        kept = [
            line for line in content.split("\n")
            if line.strip() and not _FRIEND_LEAK_RE.search(line)
        ]
        return "\n".join(kept) or "这个我不太好说呀，聊点别的吧"

    def _audit_friend(self, cid: str, user_text: str, reply: str) -> None:
        """把和朋友的对话记一份，方便主人事后查有没有说漏。"""
        try:
            path = config.DATA_DIR / "friend_audit.jsonl"
            record = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "qq": _qq_of(cid),
                "user": user_text[:200],
                "me": reply[:200],
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.debug("审计写不了：%s", exc)

    # ---------------------------------------------------------- 朋友的称呼
    def _contacts_path(self):
        return config.DATA_DIR / "contacts.json"

    def _load_contacts(self) -> dict:
        try:
            raw = self._contacts_path().read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_contacts(self) -> None:
        try:
            self._contacts_path().write_text(
                json.dumps(self._contacts, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("称呼记录存不下：%s", exc)

    def _dnd_on(self, cid: str) -> bool:
        """这个人在勿扰模式里吗（勿扰 = 不主动发，只被动回）。"""
        return bool(self._contact(cid).get("dnd"))

    def _set_dnd(self, cid: str, value: bool) -> None:
        self._set_contact(cid, dnd=bool(value))

    async def _handle_dnd_command(self, cid, mtype, uid, group_id, text: str) -> bool:
        """谁都能用的 /勿扰、/取消勿扰。"""
        raw = (text or "").strip().lower().replace(" ", "")
        on_words = ("/勿扰", "/免打扰", "/dnd", "/别烦我")
        off_words = ("/取消勿扰", "/勿扰关", "/免打扰关", "/dndoff", "/恢复")
        if raw in on_words:
            self._set_dnd(cid, True)
            log.info("[%s] 开了勿扰", cid)
            await self.send(
                mtype, uid, group_id,
                "好，我不主动找你了" + chr(10) + "你想聊的时候随时发我，我照常回",
            )
            return True
        if raw in off_words:
            self._set_dnd(cid, False)
            log.info("[%s] 关了勿扰", cid)
            await self.send(mtype, uid, group_id, "好，我又可以主动找你了")
            return True
        if raw in ("/勿扰状态", "/dnd状态"):
            await self.send(
                mtype, uid, group_id,
                "我现在" + ("不会主动找你（勿扰中）" if self._dnd_on(cid) else "会主动找你"),
            )
            return True
        return False

    def _mark_action(self, cid: str, kind: str) -> None:
        """记一笔"这件事真的发出去了"。她说话前靠这个核实。"""
        try:
            key = str(_qq_of(cid))
            self._actions.setdefault(key, {})[str(kind)] = time.time()
            self._save_state()
        except Exception as exc:
            log.debug("记台账失败：%s", exc)

    def _action_ago(self, cid: str, kind: str) -> float:
        """这个动作多少秒之前真的做过；没做过返回一个很大的数。"""
        rec = self._actions.get(str(_qq_of(cid))) or {}
        ts = float(rec.get(str(kind)) or 0)
        return time.time() - ts if ts else 1e9

    _CLAIM_RE = re.compile(
        r"(刚(刚)?唱|唱完了|唱好了|我唱了|给你唱了|刚才那首|刚发了一段|我刚发)"
    )

    def _verify_claims(self, cid: str, text: str) -> str:
        """她声称做过的事，台账里没有就不许说。

        2026-10-30 用户要求：「她说话之前得核实一下事情有没有发生」。
        """
        body = text or ""
        if not self._CLAIM_RE.search(body):
            return body
        if self._action_ago(cid, "sing") < 900:      # 15 分钟内真的发过歌
            return body

        kept = [ln for ln in body.split("\n") if ln.strip() and not self._CLAIM_RE.search(ln)]
        log.info("[%s] 她说的「做过」台账里没有，删掉相关句子（原话：%s）", cid, body[:40])
        if not kept:
            # 分清两种"没有"：能力不够，还是没唱成。
            # 小舞 27 分那种本来就不能唱，说"等我一下"是骗人的（用户 2026-10-30 指出）
            try:
                can_sing = affection.can(cid, "sing")[0]
            except Exception:
                can_sing = True
            return (
                "（我这会儿还唱不了呀，等我们再熟一点点嘛）"
                if not can_sing
                else "（我还没唱成呢，等我一下…）"
            )
        return "\n".join(kept)

    def _relation(self, cid: str) -> str:
        """这个人在她心里的**类别**：owner / lover / family / friend / other。

        `relation` 字段里存的是主人写的关系词本身（女朋友/妹妹/老大…），
        这里把它归成类别，相处方式按类别走。
        """
        if is_owner(cid):
            return "owner"
        label = str((self._contact(cid) or {}).get("relation") or "").strip()
        if not label:
            return "friend"
        return self._kind_of_label(label)

    def _relation_label(self, cid: str) -> str:
        """展示用的关系词（女朋友 / 妹妹 / 老大 / 朋友）。"""
        if is_owner(cid):
            return "主人（固定）"
        label = str((self._contact(cid) or {}).get("relation") or "").strip()
        if not label:
            return "朋友"
        return self._LABEL_ALIASES.get(label.lower(), label)

    def _gender(self, cid: str) -> str:
        """对方性别：f / m / 空（没设，就不预设）。"""
        rec = self._contact(cid) or {}
        return str(rec.get("gender") or "") or self._gender_of_label(
            str(rec.get("relation") or "")
        )

    def _set_relation(self, cid: str, relation: str, gender: str = "") -> None:
        """relation 传关系词本身（"女朋友"/"朋友"…），类别由 _kind_of_label 推。"""
        self._set_contact(cid, relation=relation, gender=gender or "")

    def _contact(self, cid: str) -> dict:
        return self._contacts.get(str(_qq_of(cid))) or {}

    def _contact_name(self, cid: str) -> str:
        return str(self._contact(cid).get("name") or "")

    def _set_contact(self, cid: str, **fields) -> None:
        key = str(_qq_of(cid))
        record = dict(self._contacts.get(key) or {})
        record.update(fields)
        self._contacts[key] = record
        self._save_contacts()

    def _touch_incoming(self, cid: str) -> None:
        """记下"他最后一次主动说话"的时间。用于"太久没回就不再打扰"。

        只在值变化超过 1 小时时才落盘，避免每条消息都写 contacts.json。
        """
        try:
            now = time.time()
            rec = self._contact(cid)
            last = float(rec.get("last_incoming") or 0)
            if now - last < 3600:
                return
            key = str(_qq_of(cid))
            record = dict(self._contacts.get(key) or {})
            record["last_incoming"] = now
            self._contacts[key] = record
            self._save_contacts()
        except Exception as exc:
            log.debug("记最后说话时间失败：%s", exc)

    @staticmethod
    def _extract_name(text: str) -> str:
        """从「你可以叫我小明」里抠出称呼。认不出来就返回空。"""
        raw = (text or "").strip().strip("。！？!?，,~～ ")
        if not raw or len(raw) > 20:
            return ""

        # 明显不是名字的回答（"随便叫什么都行"），交回去再问一次
        evasive = ("什么", "都行", "随便", "无所谓", "不知道", "你猜", "？", "?")
        if any(word in raw for word in evasive):
            return ""

        for prefix in (
            "你可以叫我", "你叫我", "以后叫我", "就叫我", "叫我做", "叫我",
            "我的名字是", "我的名字叫", "我叫", "名字是", "我是",
        ):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break

        # 只取第一个短语
        raw = re.split(r"[，。！？!?、,\s]", raw)[0].strip()
        # 尾巴上的客套话和语气词去掉：
        # 「阿杰吧」→「阿杰」、「林纸纸就好」→「林纸纸」、「叫我小明就行」→「小明」
        for _ in range(3):
            stripped = re.sub(
                r"(就好|就行了|就行|就好了|好了|就可以|可以|吧|啊|呀|哦|呢|啦|嘛|哈|呗)+$",
                "",
                raw,
            ).strip("「」\"' ")
            if stripped == raw or not stripped:
                break
            raw = stripped
        return raw[:8] if 1 <= len(raw) <= 8 else ""

    async def _compose_greeting(self) -> str:
        """第一次和人说话：先问候，再问人家希望怎么称呼。"""
        fallback = "你好呀，我是米米。我该怎么叫你呢？"
        try:
            text = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你在 QQ 上和一个刚认识的人打招呼。用米米的口吻："
                            "活泼、有点快嘴，但不要太自来熟。" + chr(10) +
                            "要求：先问一句好，然后问他希望你叫他什么。"
                            "别提任何私事，别提到自己的家人。两三行以内，一行一个意思。"
                        ),
                    },
                    {"role": "user", "content": "跟这个新朋友打个招呼吧"},
                ],
                temperature=0.9,
            )
            text = strip_markers(clean_output(text) or "")
        except Exception as exc:
            log.warning("问候语生成失败：%s", exc)
            text = ""

        if not text:
            return fallback
        if not any(word in text for word in ("怎么叫", "叫我", "称呼", "叫你")):
            text = text.rstrip() + chr(10) + "对了，我该怎么叫你好呀？"
        return text

    def _friend_rate_ok(self, cid: str) -> bool:
        """朋友每分钟最多几句。超了就先不理，免得刷屏把机器人拖死。"""
        now = time.time()
        hits = self._friend_hits.setdefault(cid, deque())
        while hits and now - hits[0] > 60:
            hits.popleft()
        limit = max(1, config.FRIEND_MSGS_PER_MIN)
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def _friend_song_ok(self, cid: str) -> bool:
        """朋友每天最多点几首歌。"""
        today = time.strftime("%Y-%m-%d")
        state = self._friend_songs.get(cid) or {}
        if state.get("date") != today:
            state = {"date": today, "count": 0}
        limit = max(0, config.FRIEND_SONGS_PER_DAY)
        if state["count"] >= limit:
            return False
        state["count"] += 1
        self._friend_songs[cid] = state
        return True

    async def _on_request(self, ev: dict) -> None:
        """别人申请加我好友。

        名单里的人自动通过；不认识的就报给主人，由主人决定。
        """
        if ev.get("request_type") != "friend":
            log.info("收到其他申请：%s", ev.get("request_type"))
            return

        # SnowLuma 和 NapCat 的字段名不一样，挨个试（踩过：取成 0，主人都不知道是谁）
        qq = 0
        for key in ("user_id", "target_id", "invitor_id", "from_id", "sender_id"):
            try:
                raw_qq = int(ev.get(key) or 0)
            except (TypeError, ValueError):
                raw_qq = 0
            if raw_qq:
                qq = raw_qq
                break
        if not qq:
            log.warning(
                "[申请] 取不到申请人的 QQ，原始事件：%s",
                json.dumps(ev, ensure_ascii=False)[:400],
            )

        flag = str(ev.get("flag") or "")
        # NapCat 会把同一个申请事件上报好几次，只处理第一次
        if not self._remember_seen(("req", qq, flag)):
            log.debug("[申请] %s 的重复事件，跳过", qq)
            return
        comment = str(ev.get("comment") or "").strip()[:40]
        log.info("[申请] %s 想加我好友，备注：%s", qq, comment or "（无）")

        owner = next(iter(config.OWNER_QQS), 0)
        known = qq in config.FRIEND_QQS

        if known and flag:
            try:
                await self.ob.set_friend_add_request(flag, True, remark=comment[:20])
                log.info("[申请] 已自动同意 %s", qq)
                if owner:
                    await self.send(
                        "private", int(owner), None,
                        f"我已经同意了 {qq} 的加好友申请\n"
                        f"（他在你设的名单里，备注：{comment or '空'}）",
                    )
            except Exception as exc:
                log.warning("[申请] 同意失败：%s", exc)
                if owner:
                    await self.send(
                        "private", int(owner), None,
                        f"{qq} 的申请我没能自动通过（可能是申请已经过期）\n"
                        f"让他取消后重新发一次就好",
                    )
            return

        # 不认识的人：记住 flag（主人事后 /同意 就能补批），再报给主人
        if flag:
            self._pending_add[qq] = flag
            self._pending_order = [x for x in self._pending_order if x != qq] + [qq]
        if owner:
            await self.send(
                "private",
                int(owner),
                None,
                (
                    f"有人想加我好友：{qq or '（没拿到 QQ 号）'}\n"
                    f"备注：{comment or '（空）'}\n"
                    + (
                        f"你要是认识他，发 /同意 {qq} 我就通过；"
                        f"或者发 /加朋友 {qq} 一起加进名单"
                        if qq
                        else "你要是认识他，直接发 /同意 我就通过"
                    )
                ),
            )

    async def _deliver_orphan_song(self, result: dict) -> None:
        """家里电脑交上来的歌，主人已经不在了（服务重启过）——补发出去。"""
        try:
            data = base64.b64decode(result.get("audio") or "")
            if not data:
                return
            ext = str(result.get("ext") or "mp3")
            folder = config.DATA_DIR / "singing"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{uuid.uuid4().hex[:12]}.{ext}"
            path.write_bytes(data)
            # 取标题失败不能连累发歌（踩过：sing 没导入 → 整段补发直接崩）
            try:
                title = sing.pretty_title(str(result.get("title") or "刚才那首"))[:30]
            except Exception:
                title = str(result.get("title") or "刚才那首")[:30]
            for cid in config.OWNER_QQS:
                if not await self._send_audio(path, "private", int(cid), None):
                    continue
                self._mark_action(f"u{cid}", "sing")
                await self.send(
                    "private",
                    int(cid),
                    None,
                    f"《{title}》唱好了～刚才我这边断了一下，差点没发出来",
                )
            # 补发也要开"会话"，否则他说「继续」没有东西可接，
            # 会被当成普通聊天（2026-10-30 实测：她回"我再翻翻看"）。
            try:
                done = int(result.get("index") or 0)
                total = int(result.get("total") or 1)
                query = str(result.get("query") or title)
                for cid in config.OWNER_QQS:
                    k = _owner_key(f"u{cid}")
                    if done + 1 < total:
                        self._song_session[k] = {
                            "query": query,
                            "next": done + 1,
                            "total": total,
                            "title": title,
                            "buffer": None,
                            "task": None,
                        }
                    try:
                        self._last_song[k] = {
                            "query": query, "title": title,
                            "index": done, "total": total, "ts": time.time(),
                        }
                        self._save_state()
                    except Exception:
                        pass
            except Exception as exc:
                log.warning("补发后开会话失败：%s", exc)

            log.info("补发了一首：%s（第 %d/%d 段，续播已就绪）",
                     title, int(result.get("index") or 0) + 1, int(result.get("total") or 1))
        except Exception as exc:
            log.warning("补发失败：%s", exc)

    def _introspect(self, topic: str, cid: str) -> str:
        """查自己服务器里的东西。只读，不动任何数据。"""
        import evolve as evolve_mod

        key = _owner_key(cid)
        name = (topic or "").strip()
        today = time.strftime("%Y-%m-%d")

        if name in ("歌单", "曲库", "歌", "songs"):
            try:
                all_songs = songlib.names()
            except Exception:
                return "曲库读不出来"
            if not all_songs:
                return "曲库是空的，一首都没做"
            text = f"曲库共 {len(all_songs)} 首：\n" + "、".join(all_songs[:40])
            if len(all_songs) > 40:
                text += f"\n（还有 {len(all_songs) - 40} 首没列出来）"
            return text

        if name in ("说说", "空间", "qzone"):
            plan = self._qzone_plan.get("main") or {}
            lines = []
            if plan.get("date") == today:
                done = plan.get("done") or []
                lines.append(
                    f"今天计划发 {len(plan.get('hours') or [])} 条，已发 {len(done)} 条"
                    f"（{', '.join(f'{h}点' for h in done) or '还没'}）"
                )
            recent = self._qzone_recent[-10:]
            if recent:
                lines.append("最近发过的：")
                lines.extend(f"· {item}" for item in reversed(recent))
            else:
                lines.append("还没发过说说")
            return "\n".join(lines)

        if name in ("记忆", "记性", "memory"):
            count = 0
            summary = ""
            lines = []
            try:
                count = len(self.mem.history(cid))
                summary = self.mem.summary(cid) or ""
            except Exception:
                pass
            lines.append(f"记忆里存着 {count} 条对话")
            if summary:
                # 只给个梗概，别把原文摘要整段倒出来（又长又像泄露）
                lines.append(f"更早的事（梗概）：{summary[:120]}…")
            try:
                other = "爸爸" if is_owner(cid) else (self._contact_name(cid) or "对方")
                for item in self.mem.history(cid)[-4:]:
                    who = other if item.get("role") == "user" else "我"
                    lines.append(f"· {who}：{str(item.get('content'))[:50]}")
            except Exception:
                pass
            return "\n".join(lines)

        if name in ("性格", "自己", "变化", "persona"):
            note = self.evolve_note or "（还没生成过，聊够 25 轮才会有）"
            return f"我最近的状态记录：\n{note}"

        if name in ("环境", "系统", "服务器", "硬件"):
            return sysinfo.system()

        if name in ("日志", "报错", "错误"):
            return sysinfo.logs()

        if name in ("文件", "数据", "占用"):
            return sysinfo.files()

        if name in ("服务", "线路", "容器", "连接"):
            return sysinfo.service()

        if name in ("素材", "内容", "家底"):
            return sysinfo.content()

        if name in ("全部", "总览", "全查"):
            parts = []
            for label, fn in (
                ("系统", sysinfo.system),
                ("服务", sysinfo.service),
                ("素材", sysinfo.content),
            ):
                try:
                    body = fn()
                except Exception as exc:
                    body = f"读不出来（{type(exc).__name__}）"
                parts.append(f"【{label}】\n{body}")
            return "\n".join(parts)

        if name in ("状态", "情况", "status"):
            lines = []
            try:
                lines.append(f"曲库 {songlib.count()} 首")
            except Exception:
                lines.append("曲库读不出来")
            plan = self._qzone_plan.get("main") or {}
            if plan.get("date") == today:
                lines.append(
                    f"今天说说计划 {len(plan.get('hours') or [])} 条，"
                    f"已发 {len(plan.get('done') or [])} 条"
                )
            watch = self._watch_plan.get(key) or {}
            if watch.get("date") == today:
                lines.append(f"今天看视频申请：{watch.get('hours')}")
            lines.append(f"已经聊了 {self._turn_count} 轮")
            lines.append(f"家里中继版本：v{int(HOME_VERSION.get('value') or 0)}")
            try:
                lines.append("性格快照：" + ("有" if evolve_mod.load() else "还没生成"))
            except Exception:
                pass
            lines.append(f"语音引擎：{config.TTS_ENGINE}")
            return "\n".join(lines)

        if name in ("能力", "功能", "会什么"):
            return (
                "我会这些：\n"
                "· 聊天、发表情包、发语音（我自己的嗓子）\n"
                "· 唱歌（曲库秒回，没有就去B站找来唱，整首分段）\n"
                "· 画画、看他发的图、上网搜图\n"
                "· 查资料、读他发的链接\n"
                "· 看B站视频（抽帧+听旁白）\n"
                "· 自己发 QQ 空间说说\n"
                "· 主动找他聊天，他没回我会再问一句"
            )

        if name in ("数据", "文件", "目录"):
            try:
                items = []
                for path in sorted(config.DATA_DIR.iterdir()):
                    if path.is_file():
                        items.append(f"· {path.name}  {path.stat().st_size // 1024} KB")
                    elif path.is_dir():
                        items.append(f"· {path.name}/（{len(list(path.iterdir()))} 个文件）")
                return "我的数据目录：\n" + ("\n".join(items) or "（空）")
            except Exception as exc:
                return f"列不出来：{exc}"

        return ""

    async def _wait_home_alive(self, seconds: int = 36) -> bool:
        """中继偶尔会断几秒（隧道重连），先等它回来，别急着报错。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            last = float(HOME_VERSION.get("ts") or 0)
            if last and (time.time() - last) < 60:
                return True
            await asyncio.sleep(3)
        return False

    @staticmethod
    def _why_failed(err: str) -> str:
        """把家里电脑抛上来的英文报错翻译成人话。"""
        low = (err or "").lower()
        if "login" in low or "sign in" in low or "需要登录" in err:
            return "这首歌要登录才能听"
        if "unsupported" in low or "format" in low or "format" in low:
            return "这个音频格式处理不了"
        if "larger" in low or "too big" in low or "size" in low:
            return "文件太大了，下不下来"
        if "timed out" in low or "timeout" in low:
            return "下载超时了"
        if "no such" in low or "not found" in low or "搜不到" in err:
            return "B站上没搜到这首歌"
        if "ffmpeg" in low:
            return "本机的音频工具出问题了"
        return ""

    async def _sing_song(
        self, cid, mtype, uid, group_id, query: str, index: int = 0
    ) -> None:
        """唱一首：先查曲库，库里没有才叫家里电脑现做。

        整首歌按 59 秒一段做，做完一段问一句，主人说继续才做下一段。
        """
        key = _owner_key(cid)
        log.info("[%s] 唱歌流程开始：%s（第 %d 段）", cid, query, index + 1)
        # 这把锁以前是个裸 bool，一旦某条路径异常退出就永远"还在唱"，
        # 之后点任何歌都被回"我还在唱呢"。现在带时间戳，超时就自动放开。
        locked_at = float(self._singing.get(key) or 0)
        if locked_at and (time.time() - locked_at) < (config.SING_TIMEOUT + 120):
            log.info("[%s] 上一首还在唱，这次忽略", cid)
            await self.send(mtype, uid, group_id, "我还在唱呢，等一下下")
            return
        if locked_at:
            log.warning("[%s] 上一首的锁卡了 %.0f 分钟，强制放开", cid, (time.time() - locked_at) / 60)
        self._singing[key] = time.time()
        try:
            # ① 第一段先查曲库：有就直接唱，不用开电脑
            if index == 0:
                if is_friend(cid) and not self._friend_song_ok(cid):
                    log.info("[%s] 朋友今天的点歌次数用完了", cid)
                    await self.send(
                        mtype,
                        uid,
                        group_id,
                        f"今天已经听够 {config.FRIEND_SONGS_PER_DAY} 首啦，明天再点好不好",
                    )
                    return
                hit = songlib.find(query)
                log.info("[%s] 曲库查找「%s」：%s", cid, query, "命中" if hit else "没有")
                if hit:
                    lib_path = songlib.path_of(hit, 0)
                    if lib_path:
                        log.info(
                            "[%s] 曲库命中：《%s》", cid, str(hit.get("title"))[:30]
                        )
                        if await self._send_audio(lib_path, mtype, uid, group_id):
                            lib_title = str(hit.get("title") or query)[:30]
                            # 记进记忆，否则她过一会儿会否认自己唱过（踩过）
                            self.mem.append(
                                cid,
                                "assistant",
                                f"（我刚发了一段语音，唱的是《{sing.pretty_title(lib_title)}》第 1 段）",
                            )
                            await self.send(
                                mtype,
                                uid,
                                group_id,
                                f"《{lib_title}》，这首我熟～" + chr(10)
                                + "还想听后面的就跟我说一声",
                            )
                            # 曲库只是第一段，后面还是要靠家里电脑做，
                            # 所以这里也得开预加载，不然说"继续"没东西可发
                            self._song_session[key] = {
                                "query": query,
                                "next": 1,
                                "total": int(songlib.total_of(hit) or 0),
                                "title": str(hit.get("title") or query),
                                "buffer": None,
                                "task": None,
                            }
                            self._song_session[key]["task"] = self._spawn(
                                self._prefetch_segment(cid, mtype, uid, group_id, 1)
                            )
                            return

            where = f"第 {index + 1} 段" if index else "开头"
            # 中继要是还跑着旧代码，唱歌会跑错（旧版只做开头 60 秒），先拦一下
            seen = int(HOME_VERSION.get("value") or 0)
            last = float(HOME_VERSION.get("ts") or 0)
            alive = bool(last) and (time.time() - last) < 90
            if not alive:
                # 隧道重连只要几秒，先等一等，别一断就报错
                log.info("[%s] 中继暂时没动静，等它回来", cid)
                alive = await self._wait_home_alive()
                if alive:
                    await self.send(
                        mtype, uid, group_id, "家里电脑刚闪了一下，我接着来～"
                    )
            if not alive:
                log.warning("[%s] 中继一直没回来（家里电脑没开？）", cid)
                await self.send(
                    mtype,
                    uid,
                    group_id,
                    "家里那台电脑没连着…" + chr(10)
                    + "把「小鲸鱼刷B站」双击打开再点歌，我就能唱了",
                )
                return
            if seen < config.HOME_MIN_VERSION:
                log.warning(
                    "[%s] 中继版本太旧：%d < %d", cid, seen, config.HOME_MIN_VERSION
                )
                await self.send(
                    mtype,
                    uid,
                    group_id,
                    "家里那个黑窗口还开着旧版程序…\n"
                    "把它关掉、重新双击一次「小鲸鱼刷B站」再点歌好不好",
                )
                return
            # 前面还有人在唱就明说，别让人干等
            ahead = int(self._sing_inflight)
            if ahead > 0 and index == 0:
                minutes = max(1, int(round(ahead * 3)))
                await self.send(
                    mtype,
                    uid,
                    group_id,
                    f"「{query}」前面还有 {ahead} 首在唱，大概要等 {minutes} 分钟～\n"
                    "唱好我立刻发你，不用一直等在这儿",
                )
            else:
                await self.send(
                    mtype, uid, group_id, f"「{query}」的{where}…我去找找，等我一下"
                )
            log.info("[%s] 准备派活给家里电脑：%s 第 %d 段", cid, query, index + 1)
            self._sing_inflight += 1
            started = time.time()
            try:
                result = await self._ask_home(
                "sing", query, timeout=config.SING_TIMEOUT, segment=index
                )
            finally:
                self._sing_inflight = max(0, self._sing_inflight - 1)
            if not isinstance(result, dict):
                self._song_session.pop(key, None)
                waited = int(time.time() - started)
                if waited >= max(30, config.SING_TIMEOUT - 30):
                    log.warning("[%s] 点歌超时：等了 %d 秒", cid, waited)
                    await self.send(
                        mtype,
                        uid,
                        group_id,
                        f"《{query}》我做了 {waited // 60} 分钟还没弄好，先不唱了"
                        + chr(10)
                        + "大概是下载卡住了或者家里电脑忙不过来，再点一次试试？",
                    )
                else:
                    await self.send(
                        mtype,
                        uid,
                        group_id,
                        f"《{query}》没送回来（家里电脑那头断了）"
                        + chr(10)
                        + "看看那个黑窗口还在不在，再点一次我重来",
                    )
                return
            if result.get("error"):
                self._song_session.pop(key, None)
                raw_error = str(result.get("error"))
                log.info("[%s] 唱歌失败：%s", cid, raw_error[:120])
                reason = self._why_failed(raw_error)

                # 「铁皮软糖的蔷薇」这种：可能是歌手+歌名，也可能整串就是歌名。
                # 搜不到就**反问一句**，别直接甩报错（用户 2026-10-30 提的）。
                asked = ""
                if "没找到" in raw_error or "搜不到" in raw_error:
                    try:
                        artist, song, _sure = sing.split_artist_song(
                            query, with_confidence=True
                        )
                    except Exception:
                        artist, song = "", ""
                    if artist and song and song != query:
                        asked = (
                            f"「{query}」我没搜到正经的版本。"
                            + chr(10)
                            + f"你是想听《{song}》（{artist} 唱的），"
                            + f"还是整串就叫《{query}》？回我一声就行"
                        )
                await self.send(
                    mtype,
                    uid,
                    group_id,
                    asked
                    or (
                        f"《{query}》没唱成：{reason}"
                        if reason
                        else f"《{query}》没唱成：{raw_error[:60]}"
                    ),
                )
                return

            audio = result.get("audio") or ""
            if not audio:
                self._song_session.pop(key, None)
                log.warning("[%s] 唱歌返回里没有音频", cid)
                await self.send(
                    mtype, uid, group_id, f"《{query}》没唱出来（家里电脑没返回音频），再点一次试试"
                )
                return

            ext = str(result.get("ext") or "mp3")
            data = base64.b64decode(audio)
            folder = config.DATA_DIR / "singing"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{uuid.uuid4().hex[:12]}.{ext}"
            path.write_bytes(data)

            done = int(result.get("index") or 0)
            total = int(result.get("total") or 1)
            log.info(
                "[%s] 唱好第 %d/%d 段《%s》（%.0f KB）",
                cid,
                done + 1,
                total,
                str(result.get("title") or "")[:40],
                len(data) / 1024,
            )

            if await self._send_audio(path, mtype, uid, group_id):
                # 同样要记进记忆
                self.mem.append(
                    cid,
                    "assistant",
                    f"（我刚发了一段语音，唱的是《{str(result.get('title') or query)[:30]}》"
                    f"第 {done + 1}/{total} 段）",
                )
                self._mark_action(cid, "sing")
            else:
                await self.send(mtype, uid, group_id, "唱完了但发不出去…")

            try:
                self._last_song[key] = {
                    "query": query, "title": str(result.get("title") or query),
                    "index": done, "total": total, "ts": time.time(),
                }
                self._save_state()
            except Exception:
                pass

            if done + 1 < total:
                self._song_session[key] = {
                    "query": query,
                    "next": done + 1,
                    "total": total,
                    "title": str(result.get("title") or query),
                    "buffer": None,
                    "task": None,
                }
                # 关键：不等主人答复，马上开始做下一段。
                # 他听这 59 秒的时间里，下一段就唱好了，说"继续"就是秒出。
                self._song_session[key]["task"] = self._spawn(
                    self._prefetch_segment(cid, mtype, uid, group_id, done + 1)
                )
                await self.send(
                    mtype,
                    uid,
                    group_id,
                    f"这是第 {done + 1}/{total} 段～还要听吗？"
                    "说句「继续」我就接着唱（我这就先唱着下一段）",
                )
            else:
                self._song_session.pop(key, None)
                await self.send(
                    mtype,
                    uid,
                    group_id,
                    f"唱完啦，整首《{str(result.get('title') or query)[:30]}》都给你了～",
                )

            # 第一段顺手存进曲库，下次点同一首能秒回开头
            if done == 0:
                try:
                    songlib.add_segment(
                        query,
                        str(result.get("title") or query),
                        int(result.get("index") or 0),
                        data,
                        ext,
                        total=int(result.get("total") or 0) or None,
                    )
                except Exception as exc:
                    log.warning("[%s] 存曲库失败：%s", cid, exc)
        except Exception as exc:
            log.warning("[%s] 唱歌出错：%s", cid, exc)
            await self.send(mtype, uid, group_id, "唱歌的时候卡住了…")
        finally:
            self._singing.pop(key, None)

    async def _compose_qzone(self) -> str:
        """现想一条说说，像随手发的；和最近发过的太像就重来。"""
        angles = (
            "说说你现在在干什么，或者刚想到的一件小事",
            "提一句今天吃的东西，或者馋什么",
            "讲一句你对爸爸的想念，但别太腻",
            "说说你刚才在手机上刷到了什么",
            "随便感慨一句，短一点",
        )
        recent = self._qzone_recent[-12:]
        avoid = ""
        if recent:
            avoid = (
                "最近已经发过这些，**换个话题、换个说法，别重复**：\n"
                + "\n".join(f"- {item}" for item in recent)
            )

        for attempt in range(3):
            try:
                messages = [
                    {"role": "system", "content": self.persona},
                    {
                        "role": "system",
                        "content": (
                            "（现在你要发一条 QQ 空间说说。"
                            + random.choice(angles)
                            + "。一到三句，像随手发的日常，不要写标记符号，"
                            "不要提「说说」「空间」这些词。）"
                        ),
                    },
                ]
                if avoid:
                    messages.append({"role": "system", "content": avoid})
                messages.append({"role": "user", "content": "（你自己想发的内容）"})

                out = await llm.chat(messages, temperature=1.2, max_tokens=120)
                text = strip_markers(clean_output(out))[:300]
                if not text:
                    continue
                if self._too_similar(text, recent):
                    log.info("说说和以前太像，重想一次（第 %d 次）", attempt + 1)
                    continue
                return text
            except Exception as exc:
                log.warning("说说内容生成失败：%s", exc)
                return ""
        # 三次都撞车，就用最后那句（至少已经换过说法）
        log.warning("三次都没想出新鲜内容，勉强发一条")
        return ""

    async def _qzone_loop(self) -> None:
        """每天随机几个整点，自己去 QQ 空间发一条。"""
        if not config.QZONE_ENABLED:
            log.info("说说功能没开（QZONE_ENABLED=false）")
            return
        log.info(
            "说说已启用：每天随机 %d~%d 条（%d~%d 点之间）",
            config.QZONE_TIMES_MIN,
            config.QZONE_TIMES_MAX,
            config.QZONE_FROM_HOUR,
            config.QZONE_TO_HOUR,
        )
        while True:
            try:
                hour = time.localtime().tm_hour
                today = time.strftime("%Y-%m-%d")
                plan = self._qzone_plan.get("main") or {}
                if plan.get("date") != today:
                    low, high = config.QZONE_FROM_HOUR, config.QZONE_TO_HOUR
                    span = list(range(low, max(low + 1, high)))
                    lo_n = min(config.QZONE_TIMES_MIN, config.QZONE_TIMES_MAX)
                    hi_n = max(config.QZONE_TIMES_MIN, config.QZONE_TIMES_MAX)
                    want = random.randint(lo_n, hi_n) if hi_n > 0 else 0
                    count = max(0, min(want, len(span)))
                    hours = sorted(random.sample(span, count)) if count else []
                    plan = {"date": today, "hours": hours, "done": []}
                    self._qzone_plan["main"] = plan
                    self._save_state()
                    log.info("今天的说说计划：%s 点各发一条（共 %d 条）", hours, count)

                if hour in plan["hours"] and hour not in plan["done"]:
                    text = await self._compose_qzone()
                    if not text:
                        continue
                    tid = await self.ob.publish_qzone(text)
                    plan["done"].append(hour)
                    self._qzone_recent = (self._qzone_recent + [text])[-30:]
                    self._save_state()
                    log.info("发了一条说说（%s）：%s", tid[:12], text[:60])
            except Exception as exc:
                log.warning("发说说出错：%s", exc)
            await asyncio.sleep(300)

    async def _compose_ask(self) -> str:
        """现编一句"想去B站看视频"的申请，别每次都说同一句。"""
        try:
            out = await llm.chat(
                [
                    {"role": "system", "content": self.persona},
                    {
                        "role": "system",
                        "content": (
                            "（现在你想去B站看个视频，先问问爸爸同不同意。"
                            "用你自己的话说，一两句，别用以前说过的句式，"
                            "别写标记符号。）"
                        ),
                    },
                    {"role": "user", "content": "（爸爸还没说话）"},
                ],
                temperature=1.15,
                max_tokens=80,
            )
            text = strip_markers(clean_output(out))
            if text:
                return text
        except Exception as exc:
            log.warning("申请语生成失败，用固定的：%s", exc)
        return random.choice(_ASK_LINES)

    def _today_plan(self, key: str) -> dict:
        """今天的"看视频申请"计划：随机挑几个整点，每天不一样。"""
        today = time.strftime("%Y-%m-%d")
        plan = self._watch_plan.get(key) or {}
        if plan.get("date") == today:
            return plan

        count = config.WATCH_VIDEO_TIMES_PER_DAY
        low, high = config.WATCH_VIDEO_FROM_HOUR, config.WATCH_VIDEO_TO_HOUR
        if count <= 0:
            hours = [config.WATCH_VIDEO_HOUR]
        else:
            span = list(range(low, max(low + 1, high)))
            count = min(count, len(span))
            hours = sorted(random.sample(span, count))

        plan = {"date": today, "hours": hours, "asked": []}
        self._watch_plan[key] = plan
        self._save_state()
        log.info("[%s] 今天的看视频计划：%s 点各问一次", key, hours)
        return plan

    async def _watch_video_loop(self) -> None:
        """每天随机几个整点，问她一句：今天能去B站看视频吗。"""
        if not config.WATCH_VIDEO_ENABLED:
            log.info("看视频申请没开（WATCH_VIDEO_ENABLED=false）")
            return
        log.info(
            "看视频申请已启用：每天随机 %d 次（%d~%d 点之间）",
            config.WATCH_VIDEO_TIMES_PER_DAY,
            config.WATCH_VIDEO_FROM_HOUR,
            config.WATCH_VIDEO_TO_HOUR,
        )
        while True:
            try:
                hour = time.localtime().tm_hour
                for cid in config.OWNER_QQS:
                    key = _owner_key(cid)
                    plan = self._today_plan(key)
                    if hour not in plan["hours"] or hour in plan["asked"]:
                        continue
                    if self._permit.get(key, {}).get("state"):
                        continue
                    if self._watching.get(key):
                        continue

                    plan["asked"].append(hour)
                    self._watch_asked[key] = plan["date"]
                    self._permit[key] = {"state": "waiting", "asked": time.time()}
                    self._save_state()

                    log.info(
                        "[%s] 申请去看视频（今天第 %d/%d 次），等主人批准",
                        key,
                        len(plan["asked"]),
                        len(plan["hours"]),
                    )
                    ask_text = await self._compose_ask()
                    await self.send("private", int(cid), None, ask_text)
            except Exception as exc:
                log.warning("申请看视频出错：%s", exc)
            await asyncio.sleep(300)

    async def _watch_random_video(self, cid, mtype, uid, group_id) -> None:
        """主人批准后：从B站热门里挑一个看，看完回来说。"""
        key = _owner_key(cid)
        # 一进来就上锁：挑视频还要几秒，这期间她绝不能编"我看完了"
        self._watching[key] = True
        payload = await self._ask_home("trending", "")
        items = (payload or {}).get("items") if isinstance(payload, dict) else None
        if not items:
            self._watching.pop(key, None)
            await self.send(mtype, uid, group_id, "B站那个列表我打不开…")
            return

        # 看过的先剔掉，免得"换个视频看"结果又看同一个
        seen = self._watched_urls.get(key, [])
        fresh = [it for it in items if it.get("url") not in seen]
        pool = fresh or items
        pick = random.choice(pool)
        if not fresh:
            log.info("[%s] 热门里的都看过了，只能重看", cid)

        seen = seen + [pick.get("url")]
        self._watched_urls[key] = seen[-10:]

        log.info("[%s] 挑中：%s", cid, pick.get("title", "")[:30])
        await self.send(
            mtype,
            uid,
            group_id,
            f"那我去看《{pick.get('title', '')[:24]}》啦，看完跟你说噢",
        )
        # 她自己挑的，主人没要图就不发图
        await self._frames_later(
            cid,
            mtype,
            uid,
            group_id,
            pick["url"],
            pick.get("title", ""),
            send_images=config.VIDEO_SEND_MODE == "always",
        )

    # 主动开口时可以用的角度，轮着来免得每次都问"在吗"
    PROACTIVE_ANGLES = (
        "说你正在做的一件小事：在收拾东西、在水边发呆、在等泡面好",
        "说刚看到/刚想到的一件小事，说完就停，不用他回应",
        "抱怨一句自己的小状况：困、饿、无聊、手机看累了",
        "说一句没头没尾的感想，像随手发的一条朋友圈",
        "说你今天想吃的东西、想听的歌、想去的海边",
        "说你在等什么，或者接下来打算干什么，很随意",
    )

    # 主动开口时绝对不许做的事（用户明确嫌像查岗、嫌编造）
    PROACTIVE_RULES = (
        "【重要】这次是你自己碎碎念，不是找他办事。",
        "· 不要问「你还醒着吗」「睡了吗」「在干嘛」「吃饭了吗」这类查岗的话。",
        "· 不要编造他说过的事——你不知道的就说你不知道，别提「上次你说的那件事」。",
        "· 一条就够，别连着问好几个问题。",
        "· 内容围绕你自己的日常和小情绪，像自言自语给他听。",
    )

    @staticmethod
    def _time_hint() -> str:
        """现在几点，该用什么口气开口。"""
        hour = time.localtime().tm_hour
        if 0 <= hour < 5:
            period, extra = (
                "凌晨",
                "这个点对方多半在睡觉：别问「还醒着吗」「怎么还不睡」「在干嘛」，"
                "就当自言自语，说一句你自己的事就好",
            )
        elif 5 <= hour < 9:
            period, extra = "清早", "可以说早安，但别太吵"
        elif 9 <= hour < 12:
            period, extra = "上午", "可以问他今天忙不忙"
        elif 12 <= hour < 14:
            period, extra = "中午", "可以问他吃饭了没"
        elif 14 <= hour < 18:
            period, extra = "下午", "可以问他这会儿在干什么"
        elif 18 <= hour < 23:
            period, extra = "晚上", "可以问他今天怎么样"
        else:
            period, extra = "深夜", "夜深了，说话轻一点，别太热闹"
        return (
            f"现在是{period}（{hour} 点多）。{extra}。"
            "说的话必须和这个时间对得上：凌晨别问「早上好」，早上别问「睡了吗」。"
        )

    async def _maybe_say_something(self) -> None:
        """到点了就看一眼：这个点该不该主动找人说话。"""
        hour = time.localtime().tm_hour
        start, end = config.PROACTIVE_QUIET_START, config.PROACTIVE_QUIET_END
        if start != end and not config.PROACTIVE_ALLOW_NIGHT:
            quiet = (start <= hour < end) if start < end else (hour >= start or hour < end)
            if quiet:
                log.debug("免打扰时段（%s 点），不主动说话", hour)
                return

        for cid in config.OWNER_QQS:
            key = _owner_key(cid)
            idle = time.time() - self._last_active.get(key, 0.0)
            log.info(
                "[%s] 主动检查：静默 %.1f 分钟，阈值 %d 分钟",
                key,
                idle / 60,
                config.PROACTIVE_IDLE_MINUTES,
            )
            if idle < config.PROACTIVE_IDLE_MINUTES * 60:
                log.info("[%s] 刚聊过，这次不打扰", key)
                continue

            if self._dnd_on(f"u{cid}"):
                log.debug("[%s] 勿扰中，不主动开口", key)
                continue

            # 他提过的作息要求（"九点前别找我"这类）在这里硬拦。
            # 靠模型自觉是没用的——它会答应，然后照发（踩过）。
            try:
                ok, why = rules.allows_proactive(f"u{cid}")
                if not ok:
                    log.info("[%s] 他的要求不让现在发（%s），这次不开口", key, why)
                    continue
            except Exception as exc:
                log.debug("读作息要求失败：%s", exc)

            angles = list(self.PROACTIVE_ANGLES)
            try:
                if affection.can_share_self(f"u{cid}"):
                    angles.append(
                        "说你刚刷到的一个视频或帖子，简单讲你看了什么、什么感觉，别问他"
                    )
            except Exception:
                pass
            angle = random.choice(angles)
            now = time.strftime("%H:%M")
            instruction = (
                f"（现在是 {now}，爸爸已经有 {idle / 3600:.1f} 小时没说话了。"
                f"轮到你先开口。{angle}。" + chr(10) + self._time_hint()
                + chr(10) + chr(10).join(self.PROACTIVE_RULES)
                + chr(10) + "直接说话，不要解释你在做什么。）"
            )
            try:
                rules.note_proactive(f"u{cid}")
            except Exception:
                pass
            log.info("[%s] 主动找他说话：%s", cid, angle[:24])
            try:
                await self.reply(
                    cid, instruction, "private", int(cid), None, None, synthetic=True
                )
            except Exception as exc:  # 主动消息失败不能影响主循环
                log.warning("[%s] 主动消息失败：%s", cid, exc)
            else:
                self._schedule_followup(_owner_key(cid))

        await self._maybe_say_to_friends()

    async def _maybe_say_to_friends(self) -> None:
        """像找主人一样，偶尔主动找朋友开个话头。"""
        if not (config.FRIEND_PROACTIVE_ENABLED and config.FRIEND_QQS):
            return

        now = time.time()
        today = time.strftime("%Y-%m-%d")

        for qq in sorted(config.FRIEND_QQS):
            if qq in config.OWNER_QQS:
                continue
            # .env 里混进过 0 这种脏号码，别真去发
            if int(qq) <= 10000:
                continue
            cid = f"u{qq}"
            key = _owner_key(cid)
            idle = now - self._last_active.get(key, 0.0)
            if idle < config.FRIEND_PROACTIVE_IDLE_MINUTES * 60:
                continue

            record = self._contact(cid)
            if record.get("dnd"):
                continue
            if record.get("unreachable"):
                continue

            # 他连续 N 天没回过话 → 不再主动找他（用户 2026-11-09 要求：5 天）。
            # 用"他上次说话"判断，不是用"我们上次说话"——否则我们自己发的消息
            # 也会把计时清零，永远停不下来。
            # 一开口就自动恢复：下次他说话时 last_incoming 会刷新。
            last_in = float(record.get("last_incoming") or 0)
            last_any = float(self._last_active.get(key) or 0)
            last_he = max(last_in, last_any)
            if last_he and (now - last_he) >= config.FRIEND_SILENT_STOP_DAYS * 86400:
                log.info(
                    "[%s] 他 %d 天没回过话了（阈值 %d 天），不再主动找他",
                    cid, int((now - last_he) // 86400), config.FRIEND_SILENT_STOP_DAYS,
                )
                continue
            try:
                ok_rule, why_rule = rules.allows_proactive(cid, now)
                if not ok_rule:
                    log.info("[%s] 他的要求不让现在发（%s），跳过", cid, why_rule)
                    continue
            except Exception as exc:
                log.debug("读作息要求失败：%s", exc)
            poke = record.get("poke") or {}
            if poke.get("date") != today:
                poke = {"date": today, "count": 0, "next": 0}

            if int(poke.get("count") or 0) >= config.FRIEND_PROACTIVE_MAX_PER_DAY:
                continue
            if now < float(poke.get("next") or 0):
                continue

            name = self._contact_name(cid)
            # 3 级起才敢主动分享自己刷到什么（还不太熟时说自己刷了啥很突兀）
            angles = list(self.PROACTIVE_ANGLES)
            try:
                if affection.can_share_self(cid):
                    angles.append(
                        "说你刚刷到的一个视频或帖子，简单讲你看了什么、什么感觉，别问他"
                    )
            except Exception:
                pass
            angle = random.choice(angles)
            instruction = (
                f"你闲着没事，想跟他说句话{('（他叫' + name + '）') if name else ''}。"
                f"{angle}。"
                "不要提任何私事、不要问他叫什么、不要说「在吗」。"
                + chr(10) + self._time_hint()
                + chr(10) + chr(10).join(self.PROACTIVE_RULES)
            )
            try:
                await self.reply(
                    cid, instruction, "private", qq, None, None, synthetic=True
                )
            except Exception as exc:
                # 发失败不记账，下次还有机会（别让网络抖动吃掉名额）
                log.warning("[%s] 主动找朋友失败：%s", cid, exc)
                continue

            try:
                rules.note_proactive(cid)
            except Exception:
                pass
            poke["count"] = int(poke.get("count") or 0) + 1
            # 越熟找得越勤（好感度低的人，她不好意思老去打扰）
            try:
                factor = float(affection.behavior(cid).get("proactive") or 1.0)
            except Exception:
                factor = 1.0
            poke["next"] = now + random.uniform(
                config.FRIEND_PROACTIVE_MIN_MINUTES * factor,
                config.FRIEND_PROACTIVE_MAX_MINUTES * factor,
            ) * 60
            self._set_contact(cid, poke=poke)
            log.info("[%s] 主动找朋友说话（今天第 %d 次）", cid, poke["count"])


    def _schedule_followup(
        self, cid: str, left: int | None = None, delay_min: int = 0, delay_max: int = 0
    ) -> None:
        """登记：如果对方过一会儿还不回，就再补一句。"""
        if not config.FOLLOWUP_ENABLED:
            return
        remaining = config.FOLLOWUP_MAX_TIMES if left is None else left
        if remaining <= 0:
            self._pending.pop(cid, None)
            return
        low = delay_min or config.FOLLOWUP_MIN_MINUTES
        high = delay_max or config.FOLLOWUP_MAX_MINUTES
        delay = random.uniform(min(low, high), max(low, high)) * 60
        self._pending[cid] = {
            "due": time.time() + delay,
            "left": remaining,
            "sent": time.time(),
        }
        log.info("[%s] 若 %d 分钟内没回，再补一句（还剩 %d 次）", cid, delay // 60, remaining)

    async def _followup_loop(self) -> None:
        """盯着那些发出去还没被回的消息，到点就接着话题补一句。"""
        if not config.FOLLOWUP_ENABLED:
            return
        while True:
            await asyncio.sleep(30)
            if not self._pending:
                continue

            hour = time.localtime().tm_hour
            start, end = config.PROACTIVE_QUIET_START, config.PROACTIVE_QUIET_END
            if start != end:
                quiet = (
                    (start <= hour < end)
                    if start < end
                    else (hour >= start or hour < end)
                )
                if quiet:
                    continue

            now = time.time()
            for cid, state in list(self._pending.items()):
                # 他回了就别再补
                if self._last_active.get(cid, 0) > state["sent"]:
                    self._pending.pop(cid, None)
                    log.debug("[%s] 他回话了，取消补话", cid)
                    continue
                if now < state["due"]:
                    continue

                instruction = (
                    "（爸爸还没回你。接着说一句：延续刚才的话题，或者追问他一句，"
                    "也可以撒个小娇。不要催他，不要重复上一句，也不要问「在吗」。）"
                )
                log.info("[%s] 他没回，主动补一句（还剩 %d 次）", cid, state["left"])
                try:
                    await self.reply(
                        cid, instruction, "private", int(cid), None, None,
                        synthetic=True
                    )
                except Exception as exc:
                    log.warning("[%s] 补话失败：%s", cid, exc)
                    self._pending.pop(cid, None)
                    continue

                state["left"] -= 1
                if state["left"] <= 0:
                    self._pending.pop(cid, None)
                else:
                    delay = (
                        random.uniform(
                            config.FOLLOWUP_MIN_MINUTES, config.FOLLOWUP_MAX_MINUTES
                        )
                        * 60
                    )
                    state["due"] = time.time() + delay
                    state["sent"] = time.time()

    def _apply_parsed(self, cid: str, parsed: dict, raw: str) -> None:
        """把抽出来的要求落地：写规则 + 排定时。

        提取成方法的原因：含糊的要求要等对方确认（pending），
        确认之后同样得排定时——之前只在"直接识别"那条路上排了，
        走确认流程的定时会丢（踩过）。
        """
        try:
            # 注意：no_sticker 也要算"有东西要存"，否则只说了"别发表情包"时
            # 这条根本不落地（踩过：她答应了但规则里什么都没有）
            if parsed.get("quiet") or parsed.get("no_proactive") \
                    or parsed.get("min_gap_min") or parsed.get("topics") \
                    or "no_sticker" in parsed:
                rules.save(cid, parsed, raw[:60])
            for item in parsed.get("remind") or []:
                try:
                    # 不信任模型给的 when（它会把"今晚11点半"算成今天11:30）。
                    # 直接取"最近的那个未来时间点"。
                    base = remind.nearest_at(str(item.get("at") or ""))
                    rec = remind.add(cid, base, item.get("note") or "", raw[:60],
                                     item.get("repeat") or "once")
                    # 自检用：一眼看清"他说了什么 → 我排到了哪一刻 → 还有多久"
                    # （之前只打 HH:MM，看不出是今天还是明天，白排查半天）
                    repeat_tag = {"daily": "，每天", "weekdays": "，工作日"}.get(
                        rec.get("repeat") or "", ""
                    )
                    log.info(
                        "[%s] 定时 ｜ 他说「%s」→ 排到 %s（%s%s）",
                        cid,
                        (raw or "").strip()[:30],
                        time.strftime("%m-%d %H:%M", time.localtime(rec["at"])),
                        remind.human_delta(rec["at"]),
                        repeat_tag,
                    )
                except Exception as exc:
                    log.debug("排定时失败：%s", exc)
        except Exception as exc:
            log.debug("落地要求失败：%s", exc)

    async def _fire_reminder(self, rec: dict) -> None:
        """按约好的时间发一条。这里不走 reply()，避免被勿扰/话题那些阀门拦掉。"""
        cid = str(rec.get("cid") or "")
        note = str(rec.get("note") or "")
        if not cid:
            return
        qq = cid[1:] if cid.startswith("u") else cid
        try:
            ask = "（到点了，这是你们约好的消息。直接说，一两行就够。）"
            if note:
                ask += f"（内容大意：{note}）"
            msgs = self.build_messages(cid, ask, False, [], None)
            msgs.append({"role": "system", "content": (
                "（这条是你们约好的定时消息。别提系统、别提定时器、"
                "别说「我按时给你发了」「我醒着呢」这类解释，就像正常开口说话。）"
            )})
            content = await llm.chat(msgs)
            content = strip_markers(content or "")
            try:
                content = affection.postprocess(cid, content)
            except Exception:
                pass
            if is_friend(cid):
                content = await self._sanitize_for_friend(content)
            lines = [ln for ln in (content or "").split("\n") if ln.strip()][:3]
            if not lines:
                lines = ["到点啦，我来喊你一声"]
            for line in lines:
                await self.send("private", qq, None, line)
                if config.SIMULATE_TYPING:
                    await asyncio.sleep(random.uniform(0.4, 0.9))
            log.info("[%s] 定时消息已发：%s", cid, note or "(无内容)")
            try:
                if remind.reschedule(rec):
                    log.info("[%s] 这是重复约定，已排下一次：%s", cid, remind.describe(cid))
            except Exception as exc:
                log.debug("排下一次失败：%s", exc)
        except Exception as exc:
            log.warning("[%s] 定时消息发失败：%s", cid, exc)

    async def _remind_loop(self) -> None:
        """每 10 秒看一次有没有到点的定时（太粗会迟到，太细白耗 CPU）。"""
        while True:
            try:
                for rec in remind.due():
                    remind.mark_done(rec)
                    await self._fire_reminder(rec)
            except Exception as exc:
                log.debug("定时检查出错：%s", exc)
            await asyncio.sleep(10)

    async def _proactive_loop(self) -> None:
        if not config.PROACTIVE_ENABLED:
            return
        low = max(1, config.PROACTIVE_MIN_MINUTES)
        high = max(low, config.PROACTIVE_MAX_MINUTES)
        while True:
            delay = random.uniform(low, high) * 60
            fire = time.strftime("%H:%M", time.localtime(time.time() + delay))
            log.info("下次可能主动说话的时间：约 %d 分钟后（%s）", delay // 60, fire)
            await asyncio.sleep(delay)
            try:
                await self._maybe_say_something()
            except Exception as exc:
                log.warning("主动消息循环出错：%s", exc)

    async def _image_query(self, cid: str, raw: str) -> str:
        """把主人的一句话变成图片搜索关键词。

        正则去词会切得乱七八糟（"再找几张鲸鱼娘的壁纸" → "再鲸鱼娘的壁纸"），
        交给模型提炼更稳：它知道该去掉哪些口语成分。
        """
        try:
            out = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "把用户的话变成一句图片搜索关键词。"
                            "只输出关键词本身，用空格分隔两三个词，五到十二个字，"
                            "不要标点、不要解释、不要「找」「图」「壁纸」之外的废话。"
                        ),
                    },
                    {"role": "user", "content": raw},
                ],
                temperature=0.2,
                max_tokens=30,
            )
        except llm.LLMError as exc:
            log.warning("[%s] 关键词提炼失败，用原话：%s", cid, exc)
            return raw.strip()

        cleaned = out.strip().strip("。，,、\"'「」")
        cleaned = cleaned.split("\n")[0].strip()
        return cleaned or raw.strip()

    async def _image_matches(self, cid: str, path, keyword: str) -> bool:
        """发出去之前让视觉模型看一眼：这张图和关键词对吗。

        搜图结果里常混进素材站照片和水印图，直接发会显得很蠢。
        每张多花一次调用，但比发错图便宜。
        """
        if not (config.IMAGE_SEARCH_VERIFY and config.VISION_ENABLED):
            return True
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        if len(raw) > config.VISION_MAX_BYTES:
            return True  # 太大就不审了，别为了审核把请求撑爆
        encoded = base64.b64encode(raw).decode()
        suffix = "png" if raw[:4] == b"\x89PNG" else "jpeg"
        try:
            answer = await llm.chat(
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"这张图片和「{keyword}」有关吗？"
                                    "只回答一个字：是 或 否。"
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/{suffix};base64,{encoded}"
                                },
                            },
                        ],
                    }
                ],
                model=config.VISION_MODEL,
                max_tokens=8,
                temperature=0.0,
            )
        except llm.LLMError as exc:
            log.warning("[%s] 图相关度审核失败，先放过：%s", cid, exc)
            return True

        verdict = answer.strip()
        ok = verdict.startswith("是")
        log.info("[%s] 审核 %s -> %s", cid, path.name, "通过" if ok else "丢掉")
        return ok

    async def _ask_home(
        self, kind: str, url: str, timeout: int = 0, segment: int = 0
    ) -> str | None:
        """把活派给家里的电脑，等它回来。

        家里那条宽带不会被 B站/抖音 当成机房 IP 拦，所以能读到东西。
        电脑没开或者没挂上，就在超时后返回 None，调用方退回直连。
        """
        if not (config.HOME_BRIDGE_ENABLED and config.HOME_TOKEN):
            return None
        if JOBS is None:
            return None

        job_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        PENDING[job_id] = future
        job = {"id": job_id, "kind": kind, "url": url}
        if kind == "sing":
            job["query"] = url
            job["segment"] = int(segment)
            job["seconds"] = config.SING_SECONDS
        if kind == "video":
            # 语音识别的 token 在服务器这边换（SDK 和密钥都在这儿），
            # 随任务下发，家里那台就不用装阿里云 SDK
            try:
                job["asr_token"] = await asyncio.to_thread(voice._aliyun_token)
                job["asr_appkey"] = config.ALIYUN_APPKEY
                job["asr_minutes"] = config.VIDEO_ASR_MINUTES
            except Exception as exc:
                log.warning("拿 ASR token 失败，这次不转写：%s", exc)
        await JOBS.put(job)
        log.info("[家里电脑] 派活 %s：%s", kind, url[:70])

        try:
            result = await asyncio.wait_for(
                future, timeout or config.HOME_TIMEOUT
            )
        except asyncio.TimeoutError:
            PENDING.pop(job_id, None)
            log.info("[家里电脑] 超时没回话，退回直连")
            return None
        except Exception as exc:
            PENDING.pop(job_id, None)
            log.warning("[家里电脑] 出错：%s", exc)
            return None

        PENDING.pop(job_id, None)
        if result:
            log.info("[家里电脑] 拿到了 %d 字", len(str(result)))
        return result

    async def _read_links(self, cid: str, text: str) -> str:
        """主人发的链接，能读的都读一遍，把内容拼成一段上下文。

        B站/抖音先走专用解析（它们得用各自的接口），其余站点直接用抓取接口。
        """
        if not config.READING_ENABLED:
            return ""
        urls = media.find_urls(text)[: config.READING_MAX_PAGES]
        if not urls:
            return ""

        parts: list[str] = []
        for url in urls:
            # B站/抖音 先让家里电脑去读（机房 IP 会被拦）
            if any(h in url for h in ("bilibili.com", "b23.tv", "douyin.com")):
                home_text = await self._ask_home("page", url)
                if home_text:
                    parts.append(home_text[: config.READING_MAX_CHARS])
                    continue

            brief = await media.describe_one(url)
            if brief:
                parts.append(brief)
                continue
            markdown = await media.fetch_markdown(url)
            if not markdown:
                log.info("[%s] 这个网页读不了：%s", cid, url[:60])
                continue
            body = markdown.strip()[: config.READING_MAX_CHARS]
            parts.append(f"从 {url[:60]} 读到：{body}")
            log.info("[%s] 读网页成功：%s（%d 字）", cid, url[:50], len(body))

        if not parts:
            return ""
        return "（我看了下这些内容：" + "；".join(parts) + "。别照念，用你自己的话说）"

    async def _frames_later(
        self, cid, mtype, uid, group_id, url, title: str = "", send_images: bool = True
    ) -> None:
        """后台去要帧：下视频慢，别把整个对话卡住。

        拿到帧就发图 + 补一句她的看法，拿不到就明说打不开——
        绝不能让她拿聊天记录编一个"看完了"。
        """
        key = _owner_key(cid)
        self._watching[key] = True
        payload = await self._ask_home("video", url)
        if not isinstance(payload, dict):
            self._watching.pop(key, None)
            await self.send(
                mtype, uid, group_id, "那个视频我这边打不开…等下再试好不好"
            )
            return
        frames = payload.get("frames") or []
        if not frames:
            reason = str(payload.get("error") or "").strip()
            log.info(
                "[%s] 后台没拿到帧。文字=%d 字，转写=%d 字，原因=%s",
                cid,
                len(str(payload.get("text") or "")),
                len(str(payload.get("transcript") or "")),
                reason[:120],
            )
            self._watching.pop(key, None)
            # 说清楚为什么，别让她显得"莫名其妙看不了"
            hint = ""
            low = reason.lower()
            if "login" in low or "cookie" in low or "登录" in reason:
                hint = "这个要登录才能看"
            elif "not available" in low or "格式" in reason:
                hint = "这个视频的格式我处理不了"
            elif "max-filesize" in low or "too large" in low:
                hint = "这个视频太大了"
            elif "超时" in reason or "timeout" in low:
                hint = "下载超时了，可能网不好"
            await self.send(
                mtype,
                uid,
                group_id,
                ("这个视频我看不了…" + hint) if hint else "这个视频我下不下来…换个短的试试？",
            )
            return

        folder = config.DATA_DIR / "frames"
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for encoded in frames[: config.VIDEO_MAX_FRAMES]:
            try:
                path = folder / f"home_{uuid.uuid4().hex}.jpg"
                path.write_bytes(base64.b64decode(encoded))
                paths.append(path)
            except (OSError, ValueError) as exc:
                log.warning("[%s] 帧落盘失败：%s", cid, exc)
        if not paths:
            self._watching.pop(key, None)
            return

        log.info("[%s] 后台拿到 %d 帧，发出去", cid, len(paths))
        if send_images:
            for path in paths:
                await self.send_segments(
                    mtype, uid, group_id, onebot.image_message(path, True)
                )
                await asyncio.sleep(0.5)
        else:
            log.info("[%s] 主人没要图，只让她自己看", cid)

        transcript = (payload.get("transcript") or "").strip()
        if transcript:
            log.info("[%s] 转写拿到 %d 字", cid, len(transcript))

        # 让她看一眼再说话
        try:
            blocks = []
            for path in paths:
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,"
                            + base64.b64encode(path.read_bytes()).decode()
                        },
                    }
                )
            remark = await llm.chat(
                self.build_messages(
                    cid,
                    "（这些是刚才那个视频里截的几帧，标题："
                    + (title or "未知")
                    + "。"
                    + (
                        "视频里说的话大概是这样：" + transcript[:3500] + "。"
                        if transcript
                        else ""
                    )
                    + "说说你看到了、听到了什么，像刚看完一样随口讲两句。）",
                    False,
                    blocks,
                ),
                model=config.VISION_MODEL,
            )
            remark = strip_markers(clean_output(remark))
            for bubble in plan_bubbles(
                remark[: config.MAX_REPLY_CHARS * 3], config.MAX_REPLY_CHARS
            ):
                await self.send(mtype, uid, group_id, bubble)
                await asyncio.sleep(random.uniform(0.4, 1.0))
            self.mem.append(cid, "assistant", remark)
        except Exception as exc:
            log.warning("[%s] 看完画面后发言失败：%s", cid, exc)
        finally:
            self._watching.pop(key, None)

    async def _compact(self, cid: str) -> None:
        """超预算时把最旧的一段压成摘要。失败就保留原文，不影响对话。"""
        need, chars, count = self.mem.needs_compaction(cid)
        if not need:
            return
        old = self.mem.peek_old(cid)
        if not old:
            return
        log.info("[%s] 触发记忆压缩：%d 条 / %d 字", cid, count, chars)
        try:
            summary = await llm.summarize(
                self.mem.summary(cid), old, config.SUMMARY_MAX_CHARS
            )
        except llm.LLMError as exc:
            log.warning("记忆压缩失败，先保留原文：%s", exc)
            return
        except Exception:
            log.exception("记忆压缩出意外，先保留原文")
            return
        self.mem.commit_compaction(cid, len(old), summary)
        log.info("[%s] 压缩完成：%d 条旧消息 → %d 字摘要", cid, len(old), len(summary))

    async def reply(
        self, cid, user_text, mtype, uid, group_id, mid, voice_mode=False,
        image_urls=None, synthetic=False
    ) -> None:
        async with self._lock(cid):
            _key = _owner_key(cid)
            _prev = float(self._last_active.get(_key) or 0)
            # 隔了多久才重新开口：超过阈值就该换话题（用户 2026-11-01 提的需求）
            self._topic_gap = (time.time() - _prev) if _prev else 0.0
            self._last_active[_key] = time.time()

            # 他"本人"说话的记录（不含她自己的主动/补话）。
            # 用来判断"多久没被回应"：连续 5 天没回就别再主动找了。
            if not synthetic:
                self._touch_incoming(cid)

            # 谁都能开关勿扰
            if not synthetic and (user_text or "").strip().startswith("/"):
                if await self._handle_dnd_command(
                    cid, mtype, uid, group_id, user_text
                ):
                    return

            # 主人可以用 /加朋友 管理名单
            if is_owner(cid) and not synthetic:
                if await self._handle_friend_command(
                    cid, mtype, uid, group_id, user_text
                ):
                    return

            # 主人发了不认识的 / 指令 → 回用法
            if (
                is_owner(cid)
                and not synthetic
                and (user_text or "").strip().startswith("/")
                and not re.match(r"^/(加朋友|加好友|添加朋友|添加好友|删朋友|删好友"
                                 r"|移除朋友|移除好友|同意|通过|拒绝|不同意|朋友|朋友列表)",
                                 (user_text or "").strip())
            ):
                await self.send(
                    mtype, uid, group_id,
                    "这个指令我不认识。发 /指令 看完整的清单。\n"
                    "/加朋友 QQ号（也可以写 /加好友）\n"
                    "/删朋友 QQ号\n"
                    "/同意 QQ号   /拒绝 QQ号\n"
                    "/朋友\n"
                    "/勿扰  /取消勿扰（勿扰时她只回你，不主动找你）",
                )
                return

            # 别人想用主人的指令：明确回绝，别让它掉进正常聊天被模型顺嘴答应
            # 朋友也可以用 /指令 看“自己能用的命令”
            if (
                not is_owner(cid)
                and not synthetic
                and (user_text or "").strip().startswith("/")
                and not re.match(
                    r"^/(指令|功能|菜单|help|帮助|叫我|称呼|rename|心情|情绪|mood|事件|约定|规矩|规则|要求)\b"
                    r"|^/(指令|功能|菜单|help|帮助|叫我|称呼|rename|心情|情绪|mood|事件|约定|规矩|规则|要求)$",
                    (user_text or "").strip().lower(),
                )
            ):
                log.info("[%s] 非主人发指令，回绝", cid)
                await self.send(mtype, uid, group_id, "这个指令我用不了，得主人自己来。发 /指令 看看我能做什么")
                return

            # 朋友有频率限制（主人不限制）
            if is_friend(cid) and not synthetic and not self._friend_rate_ok(cid):
                log.info("[%s] 朋友发得太快，这一条先不接", cid)
                return

            # 对方的号上挂着"自动回复"机器人：回它一句它就再回一句，会无限对话（踩过）。
            # 见到就整条不理，并且给这个人打上标记，之后别主动找他。
            if not synthetic and looks_auto_reply(user_text):
                # 只是"这条不回"。对方本人照样能找她、她也照样能主动找对方——
                # 人家只是开了 QQ 自动回复，不是机器人代管（用户 2026-10-30 明确要求）
                log.info("[%s] 对方开了自动回复，这条不回（%s）", cid, user_text[:30])
                return

            # 情绪：先记账（被凶/被哄/被夸），再决定这条要不要"闹别扭不理"
            if not synthetic:
                try:
                    mood.touch(cid, user_text)
                    ignore, reason = mood.should_ignore(cid, user_text)
                    if ignore:
                        mood.mark_ignored(cid)
                        log.info("[%s] 生闷气，这条先不理（%s）", cid, reason)
                        # 照样记进历史：她不是没看见，是故意不理。
                        # 不记的话下一次回复会"断片"，像失忆。
                        try:
                            self.mem.append(cid, "user", user_text)
                        except Exception:
                            pass
                        # 30% 概率丢一个气鼓鼓的短句，不然完全没动静像挂了
                        if random.random() < 0.3:
                            await self.send(mtype, uid, group_id, random.choice(("哦", "嗯", "……")))
                        return
                except Exception as exc:
                    log.debug("情绪处理失败：%s", exc)

            # 学"他提的要求"：先处理上一轮的反问确认，再看这句是不是新要求
            if not synthetic:
                try:
                    pend = rules.pending_of(cid)
                    body_now = (user_text or "").strip()
                    if pend and len(body_now) <= 8:
                        if any(w in body_now for w in rules.DENY_WORDS):
                            rules.discard_pending(cid)
                            log.info("[%s] 我说错了，他否了，先不记", cid)
                        elif any(w in body_now for w in
                                 ("上午", "早上", "早晨", "凌晨", "中午", "下午",
                                  "傍晚", "晚上", "今晚", "明早")):
                            # 他直接答了"晚上"，那就按晚上排，不用再说"对"
                            adjusted = rules.apply_period(pend, body_now)
                            self._apply_parsed(cid, adjusted, body_now[:40])
                            log.info("[%s] 按他说的时段排：%s", cid, remind.describe(cid))
                        elif body_now in rules.CONFIRM_WORDS or any(
                            body_now.startswith(w) for w in rules.CONFIRM_WORDS
                        ) or any(w in body_now for w in ("明天", "明晚", "明早", "后天")):
                            self._apply_parsed(cid, pend, pend.get("summary") or "")
                            log.info("[%s] 确认后记下要求：%s / 定时：%s",
                                     cid, rules.describe(cid), remind.describe(cid))
                    else:
                        if rules.wants_extract(user_text):
                            parsed = await rules.extract(user_text)
                            if parsed:
                                # 表情包这件事：代码判他这次是"禁"还是"放开"，
                                # 不信模型的字段（它每次都爱输出 no_sticker:false）。
                                # 他没提这件事就把字段删掉，免得把之前设的规则冲掉。
                                perm = rules.sticker_permission(user_text)
                                if perm == "allow":
                                    parsed["no_sticker"] = False
                                    log.info("[%s] 他说可以发表情包了，解禁", cid)
                                elif perm == "forbid":
                                    parsed["no_sticker"] = True
                                    log.info("[%s] 他说别发表情包，禁掉", cid)
                                else:
                                    parsed.pop("no_sticker", None)

                                # 时间点就在两小时内的话，别再反问"上午还是晚上"——
                                # 他 23:25 说「11点30叫我」显然是 5 分钟后，问一句纯属废话
                                # 上午/晚上的歧义由代码判，不信模型自己的说法：
                                # 「11点半」在 11:35 说，最近的点是今晚 23:30（翻出来的），得问；
                                # 在 09:00 或 23:25 说，最近的点就是原钟点，不用问。
                                if parsed.get("remind"):
                                    try:
                                        asks = []
                                        for item in parsed["remind"]:
                                            q = rules.time_question(
                                                user_text, str(item.get("at") or "")
                                            )
                                            if q:
                                                asks.append(q)
                                        if not asks:
                                            # 再查一种：时间点"今天已经过去了"，是不是指明天
                                            for item in parsed["remind"]:
                                                pq = rules.past_question(
                                                    user_text, str(item.get("at") or "")
                                                )
                                                if pq:
                                                    asks.append(pq)
                                                    break
                                        if asks:
                                            parsed["ambiguous"] = True
                                            parsed["summary"] = asks[0]
                                            log.info("[%s] 时间有歧义，先反问：%s", cid, asks[0])
                                        else:
                                            parsed["ambiguous"] = False
                                    except Exception as exc:
                                        log.debug("歧义判定失败：%s", exc)
                                if parsed.get("ambiguous"):
                                    # 拿不准就别硬记，反问他一句（这是他抱怨的那个毛病）
                                    rules.set_pending(cid, parsed)
                                    log.info("[%s] 要求有点含糊，先反问：%s", cid, parsed.get("summary"))
                                else:
                                    self._apply_parsed(cid, parsed, user_text or "")
                                    log.info("[%s] 记下要求：%s / 定时：%s",
                                             cid, rules.describe(cid), remind.describe(cid))
                except Exception as exc:
                    log.debug("学要求失败：%s", exc)

            # 朋友：第一次说话先问候 + 问希望怎么称呼；之后记住并按那个叫
            if is_friend(cid) and not synthetic:
                name = self._contact_name(cid)
                record = self._contact(cid)

                if not name and record.get("awaiting"):
                    guessed = self._extract_name(user_text)
                    if guessed:
                        self._set_contact(cid, name=guessed, awaiting=False)
                        log.info("[%s] 记住称呼：%s", cid, guessed)
                        await self.send(
                            mtype, uid, group_id, f"好，那我叫你{guessed}～"
                        )
                        self.mem.append(cid, "user", user_text)
                        self.mem.append(
                            cid, "assistant", f"好，那我叫你{guessed}～"
                        )
                        return
                    asked = int(record.get("ask_count") or 1)
                    if asked < 3:
                        self._set_contact(cid, ask_count=asked + 1)
                        await self.send(
                            mtype, uid, group_id, "嗯…我该怎么叫你好呀？"
                        )
                        return

                elif not name and not record.get("asked"):
                    # 还没问过称呼
                    self._set_contact(
                        cid, asked=time.time(), awaiting=True, ask_count=1
                    )
                    if self.mem.history(cid):
                        # 已经聊过的老熟人：不用重新自我介绍，顺口问一句就行
                        said = "对了，聊了这么久还不知道该怎么叫你，你希望我叫你什么呀？"
                        await self.send(mtype, uid, group_id, said)
                        self.mem.append(cid, "user", user_text)
                        self.mem.append(cid, "assistant", said)
                        log.info("[%s] 补问称呼", cid)
                        return
                    greeting = await self._compose_greeting()
                    for line in greeting.split(chr(10)):
                        if line.strip():
                            await self.send(mtype, uid, group_id, line)
                            if config.SIMULATE_TYPING:
                                await asyncio.sleep(random.uniform(0.5, 1.1))
                    self.mem.append(cid, "user", user_text)
                    self.mem.append(cid, "assistant", greeting)
                    log.info("[%s] 第一次和这个人说话，已问候并询问称呼", cid)
                    return

            # 主人让她自检 → 真去试，不是嘴上说"我可以"
            if wants_selfcheck(user_text) and not synthetic:
                log.info("[%s] 跑自检", cid)
                try:
                    report = await self._selfcheck_report()
                except Exception as exc:
                    log.warning("[%s] 自检出错：%s", cid, exc)
                    report = ""
                if report:
                    for group in _group_lines(report, 3):
                        await self.send(mtype, uid, group_id, group)
                        if config.SIMULATE_TYPING:
                            await asyncio.sleep(random.uniform(0.4, 0.9))
                    self.mem.append(cid, "user", user_text)
                    self.mem.append(cid, "assistant", "（跑了一次自检）")
                    return

            # 主人在问"你自己"的事 → 直接查真实数据，别让她嘴上敷衍
            asked_topic = wants_introspect(user_text)
            # 提到具体歌名就当点歌，别当成"查歌单"
            # （踩过：主人说"我要曲库里的光年之外"，她回了一长串歌名）
            if asked_topic == "歌单" and len(user_text or "") <= 24:
                try:
                    if songlib.mentioned(user_text):
                        asked_topic = ""
                except Exception:
                    pass
            if asked_topic and not synthetic:
                try:
                    report = self._introspect(asked_topic, cid)
                except Exception as exc:
                    log.warning("[%s] 自查 %s 失败：%s", cid, asked_topic, exc)
                    report = ""
                if report:
                    log.info("[%s] 主人问%s，直接查给他看", cid, asked_topic)
                    for group in _group_lines(report, 3):
                        await self.send(mtype, uid, group_id, group)
                        if config.SIMULATE_TYPING:
                            await asyncio.sleep(random.uniform(0.35, 0.8))
                    self.mem.append(cid, "user", user_text)
                    self.mem.append(
                        cid, "assistant", f"（查了自己的{asked_topic}：{report[:80]}）"
                    )
                    return
            # 刚唱完一段在等答复：说"继续"就发下一段（那段已经预加载好了）
            key_now = _owner_key(cid)
            # 会话没了（进程重启过 / 那次是补发的）但刚发过歌 → 用记录接上续播
            if key_now not in self._song_session:
                last = self._last_song.get(key_now) or {}
                if (
                    last
                    and time.time() - float(last.get("ts") or 0) < 3600
                    and int(last.get("index", 0)) + 1 < int(last.get("total") or 1)
                ):
                    self._song_session[key_now] = {
                        "query": str(last.get("query") or last.get("title") or ""),
                        "next": int(last.get("index", 0)) + 1,
                        "total": int(last.get("total") or 1),
                        "title": str(last.get("title") or ""),
                        "buffer": None,
                        "task": None,
                    }
                    log.info("[%s] 用上次那首歌的记录恢复续播：%s 第 %d 段",
                             cid, self._song_session[key_now]["query"],
                             int(last.get("index", 0)) + 2)

            session = self._song_session.get(key_now)
            if session and not synthetic and not self._singing.get(key_now):
                reply_text = (user_text or "").strip()
                if wants_stop_song(reply_text):
                    session["cancelled"] = True
                    self._song_session.pop(_owner_key(cid), None)
                    log.info("[%s] 主人不听了", cid)
                    await self.send(mtype, uid, group_id, "好呀，那这首就先到这儿")
                    return
                if looks_new_song_request(reply_text):
                    # 他在点另一首歌：把上一首的会话结束掉，别继续发原曲
                    session["cancelled"] = True
                    self._song_session.pop(_owner_key(cid), None)
                    log.info("[%s] 他改点新歌了（%s），上一首收掉，走正常点歌流程",
                             cid, reply_text[:20])
                elif wants_more_song(reply_text):
                    log.info("[%s] 主人要听下一段：%s", cid, reply_text[:20])
                    self._spawn(self._sing_continue(cid, mtype, uid, group_id))
                    return

            # 她刚申请去看视频，这条可能就是主人的答复
            permit = self._permit.get(_owner_key(cid))
            if permit and permit.get("state") == "waiting" and not synthetic:
                reply_text = (user_text or "").strip()
                if any(word in reply_text for word in _NEGATIVE):
                    self._permit.pop(_owner_key(cid), None)
                    log.info("[%s] 主人不同意，放弃", cid)
                    await self.send(mtype, uid, group_id, "那好吧，今天就不看啦")
                    return
                if any(word in reply_text for word in _AFFIRM):
                    self._permit[_owner_key(cid)] = {"state": "watching"}
                    log.info("[%s] 主人批准了，开始挑视频", cid)
                    await self.send(mtype, uid, group_id, "好耶，那我去了")
                    self._spawn(
                        self._watch_random_video(cid, mtype, uid, group_id)
                    )
                    return

            frame_paths: list = []
            asked_for_frames = False

            # 视频还在下/转写，这时候问她"讲了什么"，她只能说还在看
            watching_key = _owner_key(cid)
            if self._watching.get(watching_key):
                probe = (user_text or "").strip()
                if any(
                    word in probe
                    for word in ("看完", "视频", "讲了", "说说什么", "内容", "看了吗")
                ):
                    log.info("[%s] 视频还在处理，先回一句", cid)
                    await self.send(
                        mtype, uid, group_id, "还在看呢，视频有点长，等我一下嘛"
                    )
                    return

            # 好感度：今天第一次说话、聊得多、被夸被骂，都在这里记
            if not synthetic:
                try:
                    for note in affection.touch_message(cid) + affection.touch_text(cid, user_text):
                        log.debug("[%s] 好感度 %s", cid, note)
                except Exception as exc:
                    log.debug("好感度记账失败：%s", exc)

            # 主人直接叫她自己去看（不走申请流程）
            if (
                wants_watch_video(user_text)
                and affection.can(cid, "watch")[0]
                and not self._watching.get(watching_key)
            ):
                log.info("[%s] 主人让她自己去看视频", cid)
                await self.send(
                    mtype, uid, group_id, random.choice(_GO_WATCH_LINES)
                )
                self._spawn(
                    self._watch_random_video(cid, mtype, uid, group_id)
                )
                return
            try:
                if config.SIMULATE_TYPING:
                    await asyncio.sleep(
                        random.uniform(config.REPLY_PAUSE_MIN, config.REPLY_PAUSE_MAX)
                    )  # 假装在看你发的话
                blocks = await self._image_blocks(cid, image_urls)

                # 视频链接：文字快速拿，帧放后台慢慢来，别把对话卡住
                video_note = None
                home_text = None
                if config.VIDEO_FRAMES_ENABLED and not blocks:
                    video_url = media.find_video_url(user_text)
                    if video_url:
                        payload = await self._ask_home("page", video_url)
                        if isinstance(payload, str):
                            home_text = payload
                        # 下视频几十秒起步，排到后台，她先正常回话
                        self._spawn(
                            self._frames_later(
                                cid,
                                mtype,
                                uid,
                                group_id,
                                video_url,
                                home_text or "",
                                send_images=(
                                    config.VIDEO_SEND_MODE == "always"
                                    or wants_frames(user_text)
                                ),
                            )
                        )
                        log.info("[%s] 抽帧排到后台，先回文字", cid)

                # 发的是链接的话，先读一下内容，让她接得住话
                enriched = user_text
                if home_text:
                    link_context = home_text
                else:
                    try:
                        link_context = await self._read_links(cid, user_text)
                    except Exception as exc:
                        log.warning("[%s] 读链接出错：%s", cid, exc)
                        link_context = ""
                if link_context:
                    enriched = user_text + "\n\n" + link_context

                content = await llm.chat(
                    self.build_messages(
                        cid, enriched, voice_mode, blocks, video_note
                    ),
                    model=config.VISION_MODEL if blocks else None,
                )

                # 她想上网查的话，搜一遍，让她拿着结果重说一次
                if config.SEARCH_ENABLED:
                    hit = _SEARCH_RE.search(content)
                    if hit:
                        query = hit.group(1).strip()
                        content = _SEARCH_RE.sub("", content).strip() or "（我查一下）"
                        found = await media.search(query, config.SEARCH_COUNT)
                        log.info("[%s] 搜索「%s」→ %d 条结果", cid, query, len(found))
                        if found:
                            context = (
                                "我刚上网查过了，结果如下。别念链接，用你自己的话把结论说出来：\n"
                                + media.format_results(found)
                            )
                            content = await llm.chat(
                                self.build_messages(
                                    cid, enriched, voice_mode, blocks, context
                                ),
                                model=config.VISION_MODEL if blocks else None,
                            )
                            # 第二次回复里她常又写一遍标记，必须再清一次
                            content = strip_markers(content)
            except llm.LLMError as exc:
                log.error("生成失败：%s", exc)
                await self.send(
                    mtype, uid, group_id, "呜…我这边的脑子连不上了，等会儿再问我一次？（细节在日志里）"
                )
                return
            except Exception:
                log.exception("生成时出了意外")
                await self.send(mtype, uid, group_id, "我刚刚卡了一下，你再说一遍？")
                return

            # 排查用：LOG_LEVEL=DEBUG 时能看到模型原话和清洗后的话
            log.debug("[%s] 原始输出：%r", cid, content[:400])

            # 模型自己判断"主人让我去看视频"。
            # 必须放在 strip_markers 之前——以前放在后面，标记早被清掉了，
            # 所以这条路一直没生效，全靠关键词兜底（2026-10 才发现）
            go_watch = False
            if config.VIDEO_FRAMES_ENABLED and _WATCH_RE.search(content):
                go_watch = True
                content = _WATCH_RE.sub("", content).strip()
                log.info("[%s] 她自己请求去看视频（标记）", cid)

            # 模型想画图时写 [[画:描述]]。
            # 必须在 strip_markers 之前抠出来——标记里有 _DRAW_RE，先 strip 就永远找不到，
            # 症状是"她答应了要画，然后就没下文了"（2026-10-27 才发现，和 [[看片]] 同一个坑）
            draw_prompt = None
            if config.IMAGE_ENABLED and _DRAW_RE.search(content):
                found = _DRAW_RE.search(content)
                draw_prompt = found.group(1).strip()
                content = _DRAW_RE.sub("", content).strip()
                log.info("[%s] 想画：%s", cid, draw_prompt)

            # 兜底：任何路径漏下来的标记都不许发到聊天里
            content = strip_markers(content)

            content = clean_output(content) or "（这次没憋出话来…）"

            # 好感度低的人，说出来的话要"降温"（少说两句、别太软）。
            # 光靠提示词压不住人设，这一步是确定性的。
            try:
                content = affection.postprocess(cid, content)
            except Exception as exc:
                log.debug("好感度降温失败：%s", exc)

            # 说话前核实：她说的"我唱过了"必须台账里真有（用户 2026-10-30 要求）
            try:
                content = self._verify_claims(cid, content)
            except Exception as exc:
                log.debug("核实已做过的事失败：%s", exc)

            # 话题禁区：他明确说过"别老问我吃了没"这类。踩到了就让她重说一次。
            # 注意 check_topic 会把"他自己先提了这个话题"的情况放行。
            try:
                hit = rules.check_topic(cid, content, user_text)
                if hit:
                    rules.note_topic_hit(cid, hit)
                    if synthetic:
                        # 主动开口踩了禁区：这条干脆不发，比发了再道歉好
                        log.info("[%s] 主动开口踩到话题禁区「%s」，这次不发了", cid, hit)
                        return
                    log.info("[%s] 回复踩到话题禁区「%s」，让她换个说法", cid, hit)
                    retry = self.build_messages(cid, user_text, voice_mode, blocks, video_note)
                    retry.append({"role": "system", "content": (
                        f"（换个说法重说一遍。刚才那句提到了「{hit}」，"
                        "他不想聊这个，别再碰，也别提自己刚才说过什么。）"
                    )})
                    again = await llm.chat(retry, model=config.VISION_MODEL if blocks else None)
                    again = strip_markers(again or "")
                    if again and not rules.check_topic(cid, again, user_text):
                        content = affection.postprocess(cid, again)
                    else:
                        # 重说还是踩：把那几句删掉，别硬发
                        kept = [
                            ln for ln in (content or "").split("\n")
                            if ln.strip() and hit not in ln
                        ]
                        content = "\n".join(kept) or "嗯，你说"
                        log.info("[%s] 重说还踩禁区，删掉相关句子", cid)
            except Exception as exc:
                log.debug("话题禁区检查失败：%s", exc)

            # 跟朋友说话：把提到爸爸的内容硬过滤掉（提示词挡不住的最后一层）
            if is_friend(cid):
                before = content
                content = await self._sanitize_for_friend(content)
                if content != before:
                    log.info("[%s] 朋友对话已过滤私密内容", cid)
            if config.STRIP_ACTIONS:
                content = strip_actions(content) or "嗯"

            # 朋友的对话留个底，主人事后能查她有没有说漏
            if is_friend(cid):
                self._audit_friend(cid, user_text, content)

            # 兜底：模型没写标记，但对方明确要图，那就直接画
            if config.IMAGE_ENABLED and not draw_prompt and wants_draw(user_text):
                draw_prompt = user_text
                log.info("[%s] 对方明确要图，直接开画：%s", cid, user_text[:40])

            # 好感度不到，画画这条就不往下走（她要给个像样的理由，不能像系统报错）
            if draw_prompt and not affection.can(cid, "draw")[0]:
                # 只有"对方真的点名要图"才解释；她自己想画就悄悄不画。
                # 否则每次她心里一动，对方就收到一条一模一样的报错（踩过）。
                asked_draw = wants_draw(user_text)
                log.info(
                    "[%s] 好感度不够画画（%.1f），%s",
                    cid, affection.score(cid), "对方点了，要解释" if asked_draw else "她自己想画，静默跳过",
                )
                if asked_draw and affection.should_say_locked(cid, "draw"):
                    await self.send(mtype, uid, group_id, affection.locked_reply(cid, "draw"))
                draw_prompt = None

            # 唱歌：模型写 [[唱:歌名]]，或者主人直接说"唱一首 X"
            sing_query = None
            if config.SING_ENABLED:
                # 主人原话优先：模型偶尔会自作主张唱别的歌
                # （踩过：主人说"曲库里的光年之外"，它写了 [[唱:晴天]]）
                # **她自己碎碎念时绝不能从这里触发唱歌**：
                # 2026-11-01 她主动说话的提示词里有"想听的歌"，
                # 结果被当成点歌，去抽了个歌名《的歌、想去的海边。》，
                # 家里电脑没开 → 突然冒出一句"把「小鲸鱼刷B站」打开"。
                asked_song = (
                    pick_song_name(user_text)
                    if (not synthetic and wants_sing(user_text))
                    else ""
                )
                if not asked_song and len(user_text or "") <= 24:
                    # 「我要曲库里的光年之外」这种说法认不出来，
                    # 但库里真有这首歌 —— 那就唱它
                    hit_song = songlib.mentioned(user_text)
                    if hit_song:
                        asked_song = str(hit_song.get("query") or "")
                        log.info("从话里认出了库里的歌：《%s》", asked_song)
                found = _SING_RE.search(content)
                marker_song = ""
                if found:
                    marker_song = found.group(1).strip()
                    content = _SING_RE.sub("", content).strip()

                if asked_song:
                    sing_query = asked_song
                    if marker_song and marker_song != asked_song:
                        log.warning(
                            "模型想唱《%s》，主人说的是《%s》，听主人的",
                            marker_song,
                            asked_song,
                        )
                elif marker_song:
                    sing_query = marker_song
                if sing_query and not affection.can(cid, "sing")[0]:
                    # 同上：对方点名要听 vs 她自己想哼，两回事。
                    asked_sing = wants_sing(user_text) or bool(asked_song)
                    log.info(
                        "[%s] 好感度不够唱歌（%.1f），%s",
                        cid, affection.score(cid), "对方点了，要解释" if asked_sing else "她自己想唱，静默跳过",
                    )
                    if asked_sing and affection.should_say_locked(cid, "sing"):
                        await self.send(mtype, uid, group_id, affection.locked_reply(cid, "sing"))
                    sing_query = None

                if sing_query:
                    log.info("[%s] 要唱歌：%s", cid, sing_query)
                    try:
                        affection.touch_done(cid, "唱歌")
                        mood.pleasant(cid, "陪她唱歌")
                    except Exception:
                        pass

            # 她自己想用声音说：模型写了 [[语音]]
            if config.TTS_ENABLED and _VOICE_RE.search(content):
                content = _VOICE_RE.sub("", content).strip()
                if content:
                    voice_mode = True
                    log.info("[%s] 她选择用语音说：%s", cid, content[:40])

            # 她要查自己服务器里的东西（只读）
            # 自查是她自己的体检报告，只给主人看。
            # 2026-10-29 踩过：朋友说一句「不记得了吗」，模型写了 [[查:记忆]]，
            # 代码没判身份，把整段旧对话摘要（还带着「爸爸」）倒进了朋友聊天里。
            found = _INTROSPECT_RE.search(content) if is_owner(cid) else None
            if found:
                content = _INTROSPECT_RE.sub("", content).strip()
                topic = found.group(1).strip()
                try:
                    report = self._introspect(topic, cid)
                except Exception as exc:
                    log.warning("[%s] 自查 %s 出错：%s", cid, topic, exc)
                    report = ""
                if report:
                    # 双保险：万一以后有人放开上面的门禁，这里也要过滤
                    if is_friend(cid):
                        report = await self._sanitize_for_friend(report)
                    # 一行一条会把聊天刷满（实测一次刷 7 条），合并成 2~3 条
                    for group in _group_lines(report, 3):
                        await self.send(mtype, uid, group_id, "\n".join(group))
                        if config.SIMULATE_TYPING:
                            await asyncio.sleep(random.uniform(0.35, 0.8))
                    log.info("[%s] 自查：%s", cid, topic)
                    self.mem.append(cid, "user", user_text)
                    self.mem.append(cid, "assistant", f"（查了自己的{topic}：{report[:80]}）")
                    return

            # 她要把曲库全清单报出来（必须在 content 生成之后判断）
            if _SONGLIST_RE.search(content):
                content = _SONGLIST_RE.sub("", content).strip()
                try:
                    all_songs = songlib.names()
                except Exception:
                    all_songs = []
                if not all_songs:
                    await self.send(mtype, uid, group_id, "我曲库还是空的呢，一首都没做")
                else:
                    lines = [f"我曲库里有 {len(all_songs)} 首："]
                    for start in range(0, len(all_songs), 8):
                        lines.append("、".join(all_songs[start : start + 8]))
                    for line in lines:
                        await self.send(mtype, uid, group_id, line)
                        if config.SIMULATE_TYPING:
                            await asyncio.sleep(random.uniform(0.4, 0.9))
                log.info("[%s] 报了一次曲库清单（%d 首）", cid, len(all_songs))
                self.mem.append(cid, "user", user_text)
                self.mem.append(
                    cid, "assistant", f"（报了曲库清单，共 {len(all_songs)} 首）"
                )
                return

            # 网上找图
            find_query = None
            if config.IMAGE_SEARCH_ENABLED:
                found = _FINDIMG_RE.search(content)
                if found:
                    find_query = found.group(1).strip()
                    content = _FINDIMG_RE.sub("", content).strip()
                elif wants_find_image(user_text):
                    find_query = user_text
                if find_query:
                    log.info("[%s] 去网上找图：%s", cid, find_query[:40])

            # 他这条消息本来就带了表情包 → 这次一个都不回
            _no_sticker_now = cid in self._suppress_sticker
            self._suppress_sticker.discard(cid)

            # 把表情包标记抠出来，正文里不显示
            sticker_wants = (
                _STICKER_RE.findall(content)
                if (config.STICKERS_ENABLED and not _no_sticker_now)
                else []
            )
            if sticker_wants:
                # 模型偶尔写一个和这句话完全相反的情绪，纠正掉
                fixed = []
                for want in sticker_wants:
                    if emotion.clash(cid, content, want):
                        better = emotion.consistent(cid, content)
                        log.info(
                            "[%s] 贴图「%s」和这句话不搭，换成「%s」", cid, want, better
                        )
                        want = better
                    fixed.append(want)
                sticker_wants = [w for w in fixed if w]
                content = _STICKER_RE.sub("", content).strip()
                log.info("[%s] 想发表情包：%s", cid, sticker_wants)
                if not content and not voice_mode:
                    content = "（发了个表情）"

            # 他明确说过"别发表情包"→ 一律不发；只有他这次又主动要，才把约定撤掉
            try:
                if sticker_wants and rules.no_sticker(cid):
                    if wants_sticker(user_text):
                        rules.clear_no_sticker(cid)
                        log.info("[%s] 他这次主动要表情包，把「别发」的约定撤了", cid)
                    else:
                        log.info("[%s] 他说过别发表情包，这次不发（丢 %d 个标记）",
                                 cid, len(sticker_wants))
                        sticker_wants = []
            except Exception as exc:
                log.debug("表情包禁区判断失败：%s", exc)

            # 对方明确要表情包 → 必须发，不等概率也不管心情平不平
            # 刚发过图（30 分钟内）→ "我还要""还要"也算追要
            recent = (time.time() - float(self._last_sticker_at.get(cid) or 0)) < 1800
            asked_more = wants_more_stickers(user_text, contextual=recent)
            # 注意："别发表情包"里也有"发表情包"四个字，会被 wants_sticker() 当成"他要"，
            # 于是刚设的禁止立刻被下面的"撤销约定"清掉（踩过）。
            # 所以这里必须排除"禁止"语气的句子。
            _perm_now = rules.sticker_permission(user_text)
            if (
                not sticker_wants
                and config.STICKERS_ENABLED
                and _perm_now != "forbid"
                and (wants_sticker(user_text) or asked_more)
            ):
                # 他既然主动要，说明"别发表情包"那条不算了，顺手撤掉
                # （否则下次又发不出去，他会觉得她根本没听）
                # 但如果这句本身就是在说"别发"，绝不能撤（上面的 _perm_now 已经挡了，
                # 这里再保一道）
                try:
                    if rules.no_sticker(cid) and _perm_now != "forbid":
                        rules.clear_no_sticker(cid)
                        log.info("[%s] 他主动要表情包，撤掉「别发」的约定", cid)
                except Exception:
                    pass
                forced = emotion.consistent(cid, content) or "开心"
                sticker_wants = [forced]
                log.info(
                    "[%s] 对方%s表情包，直接发一个（%s）",
                    cid,
                    "追要" if asked_more else "要",
                    forced,
                )

            # 模型没主动发时，按概率随机补一张，让它显得真的爱发
            if (
                not sticker_wants
                and not voice_mode
                and config.STICKERS_ENABLED
                and not rules.no_sticker(cid)
                and not _no_sticker_now
                and config.STICKER_PROBABILITY > 0
                and random.random() < config.STICKER_PROBABILITY
            ):
                # 不再随机挑分类：按"这句话的语气 + 此刻心情"挑，免得嘴上开心手甩生气
                picked = emotion.consistent(cid, content)
                if picked:
                    sticker_wants = [picked]
                    log.info(
                        "[%s] 按情绪补一张表情包：%s（%s）",
                        cid,
                        picked,
                        emotion.stats(cid),
                    )
            log.debug("[%s] 清理后：%r", cid, content[:400])

            if voice_mode:
                if not synthetic:
                    self.mem.append(cid, "user", user_text)
                self.mem.append(cid, "assistant", content)
                await self.send_voice(cid, content, mtype, uid, group_id, mid)
                await self._compact(cid)
                # 语音发完**不能直接 return**：要画、要唱、找图、看片这些动作
                # 全都在这下面，一 return 就全跳过。
                # 症状：说「唱 AIZO」她只回「我去找找」，然后永远没下文（2026-10-27 踩到）
                bubbles = []
            else:
                content = strip_last_period(ensure_bubbles(content))
                clipped = content[: config.MAX_REPLY_CHARS * 3]
                bubbles = (
                    plan_bubbles(clipped, config.MAX_REPLY_CHARS)
                    if config.MULTI_BUBBLE
                    else split_message(clipped, config.MAX_REPLY_CHARS)
                )

                if not synthetic:
                    self.mem.append(cid, "user", user_text)
                self.mem.append(cid, "assistant", content)

            # 聊完这几句，后台看看有没有值得长期记住的（不阻塞回复）
            if not synthetic:
                self._spawn(self._neuron_learn(cid, user_text, content))

            for index, bubble in enumerate(bubbles):
                if config.SIMULATE_TYPING:
                    # 按真实打字速度算：她"打"完这句话才发出去
                    typing = 0.8 + len(bubble) / max(1.0, config.TYPING_CHARS_PER_SEC)
                    typing = min(typing, config.TYPING_BUBBLE_MAX)
                    typing = max(typing, config.TYPING_BUBBLE_MIN)
                    typing += random.uniform(
                        config.BUBBLE_GAP_MIN, config.BUBBLE_GAP_MAX
                    )
                    await asyncio.sleep(typing)
                quote = mid if (index == 0 and config.QUOTE_REPLY and mtype == "private") else None
                await self.send(mtype, uid, group_id, bubble, quote)

            # 她自己申请去看视频：先把话说出去，再去挑
            if go_watch:
                self._spawn(
                    self._watch_random_video(cid, mtype, uid, group_id)
                )

            # 模型觉得主人想看画面，会写 [[截图]]
            if _SHOT_RE.search(content):
                asked_for_frames = True
                content = _SHOT_RE.sub("", content).strip()
                log.info("[%s] 她想把截帧发出去", cid)

            # 网上找图：搜出来下载好，再发过去
            if find_query:
                keyword = await self._image_query(cid, find_query)
                if not keyword:
                    keyword = find_query.strip()
                log.info("[%s] 搜图关键词：%r", cid, keyword)

                candidates = await media.find_images(
                    keyword, config.IMAGE_SEARCH_MAX
                )
                sent = 0
                for url in candidates:
                    if sent >= config.IMAGE_SEARCH_MAX:
                        break
                    log.info("[%s] 试下载：%s", cid, url[:90])
                    path = await media.download_image(url)
                    if path is None:
                        log.info("[%s] 跳过（太小或不是图片）", cid)
                        continue
                    if not await self._image_matches(cid, path, keyword):
                        continue
                    await self.send_segments(
                        mtype, uid, group_id, onebot.image_message(path, True)
                    )
                    sent += 1
                    log.info("[%s] 发了一张网图：%s", cid, path.name)

                if sent == 0:
                    log.info("[%s] 没找到合适的图", cid)
                    await self.send(
                        mtype, uid, group_id, "网上没找到合适的图…"
                    )

            # 画图：先把对话翻译成画面描述，再生成
            if draw_prompt:
                # 判断"画的是不是她自己"：标记里有名字，或者主人原话就是"画你"
                asked_self = ("画" in user_text) and any(
                    k in user_text for k in ("你", "自己", "鲸鲸", "米米", "小鲸鱼")
                )
                self_is = asked_self or any(
                    k in draw_prompt for k in config.IMAGE_SELF_KEYS
                )
                visual = await self._image_prompt(cid, draw_prompt)
                log.info(
                    "[%s] 画面描述：%s（画自己=%s）", cid, visual, self_is
                )
                try:
                    drawn = await imagegen.generate(visual, self_ref=self_is)
                except imagegen.ImageError as exc:
                    log.error("[%s] 生图失败：%s", cid, exc)
                    await self.send(mtype, uid, group_id, f"画不出来…（{exc}）")
                else:
                    await self.send_segments(
                        mtype, uid, group_id, onebot.image_message(drawn, True)
                    )

            # 唱歌要几十秒，先在后台跑，别卡着回复
            if sing_query:
                self._spawn(
                    self._sing_song(cid, mtype, uid, group_id, sing_query)
                )

            # 只有主人要的时候才把帧发回去
            mode = config.VIDEO_SEND_MODE
            if (
                frame_paths
                and mode != "never"
                and (mode == "always" or asked_for_frames or wants_frames(user_text))
            ):
                log.info("[%s] 把 %d 张截帧发回去", cid, len(frame_paths))
                for frame in frame_paths:
                    if config.SIMULATE_TYPING:
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                    await self.send_segments(
                        mtype, uid, group_id, onebot.image_message(frame, True)
                    )

            # 文字发完，再补表情包
            for want in sticker_wants[: config.STICKER_MAX_PER_REPLY]:
                emotion_label = want
                path = stickers.pick(emotion_label)
                if path is None:
                    log.info("[%s] 没有「%s」这类表情包，跳过", cid, emotion_label)
                    continue
                self._last_sticker_at[cid] = time.time()
                log.info("[%s] 发表情包 %s（%s）", cid, path.name, emotion_label)
                await self.send_segments(
                    mtype, uid, group_id, onebot.image_message(path, True)
                )
                if config.SIMULATE_TYPING:
                    await asyncio.sleep(random.uniform(0.4, 1.0))

            # 用这句话更新她的心情（下一轮的说法和表情包都会受影响）
            try:
                emotion.update(cid, content)
                log.debug("[%s] 心情：%s", cid, emotion.stats(cid))
            except Exception as exc:
                log.debug("更新心情失败：%s", exc)

            # 正常说完话也登记一次"待追"：主人没接话就过一阵再喊他
            if not synthetic and not voice_mode and config.FOLLOWUP_AFTER_REPLY and is_owner(cid):
                self._schedule_followup(
                    _owner_key(cid),
                    left=1,
                    delay_min=config.FOLLOWUP_REPLY_MIN,
                    delay_max=config.FOLLOWUP_REPLY_MAX,
                )

            # 聊够轮数就悄悄更新一次性格快照（只对主人，朋友不参与她的成长）
            if not synthetic and is_owner(cid):
                self._spawn(self._maybe_evolve(cid))

            # 回完再检查记忆预算，用户不会被压缩拖慢
            await self._compact(cid)

    # ---------- HTTP ----------
    async def on_event(self, request: web.Request) -> web.Response:
        try:
            ev = await request.json()
        except Exception:
            log.warning("收到非 JSON 上报，忽略")
            return web.json_response({"status": "ok"})
        self._spawn(self._safe_handle(ev))
        return web.json_response({"status": "ok"})

    async def _safe_handle(self, ev) -> None:
        try:
            await self.handle_event(ev)
        except Exception:
            log.exception("处理事件出错")


async def _status_page(request: web.Request) -> web.Response:
    """一个小小的状态页：她今天什么心情、几点要干嘛、最近说了什么。"""
    bot: WhaleBot = request.app["bot"]
    import html as htmlmod

    try:
        online = await bot.ob.get_status()
    except Exception:
        online = False

    key = _owner_key(next(iter(config.OWNER_QQS))) if config.OWNER_QQS else "main"
    qplan = bot._qzone_plan.get("main") or {}
    wplan = bot._watch_plan.get(key) or {}
    _hv = int(HOME_VERSION.get("value") or 0)
    _hts = float(HOME_VERSION.get("ts") or 0)
    if not _hts or (time.time() - _hts) >= 60:
        home_txt = "<span class='bad'>没连上</span>"
    elif _hv < config.HOME_MIN_VERSION:
        home_txt = f"<span class='bad'>旧版 v{_hv}，要重启窗口</span>"
    else:
        home_txt = f"<span class='ok'>在线 v{_hv}</span>"

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    hour = time.localtime().tm_hour

    def esc(text) -> str:
        return htmlmod.escape(str(text or ""))

    def hour_list(hours, done) -> str:
        if not hours:
            return "<span class='muted'>今天没排</span>"
        out = []
        for item in hours:
            mark = "done" if item in done else ("now" if item == hour else "")
            out.append(f"<span class='pill {mark}'>{item}:00</span>")
        return " ".join(out)

    qdone = qplan.get("done") or []
    wdone = wplan.get("asked") or []
    recent = "".join(
        f"<li>{esc(item)}</li>" for item in reversed(bot._qzone_recent[-10:])
    ) or "<li class='muted'>还没发过</li>"
    note = esc(bot.evolve_note) or "<span class='muted'>还没生成（聊够 25 轮会有）</span>"

    page = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小鲸鱼状态</title>
<meta http-equiv="refresh" content="30">
<style>
 body{{background:#0f1115;color:#e6e6e6;font:15px/1.7 system-ui,-apple-system,"Microsoft YaHei",sans-serif;margin:0;padding:28px}}
 .wrap{{max-width:760px;margin:0 auto}}
 h1{{font-size:20px;margin:0 0 4px}}
 .sub{{color:#8b93a1;font-size:13px;margin-bottom:22px}}
 .card{{background:#171a21;border:1px solid #232936;border-radius:12px;padding:18px 20px;margin-bottom:14px}}
 .card h2{{font-size:14px;color:#8b93a1;font-weight:normal;margin:0 0 10px;letter-spacing:.5px}}
 .pill{{display:inline-block;background:#232936;color:#9aa4b2;border-radius:999px;padding:2px 10px;margin:2px 4px 2px 0;font-size:13px}}
 .pill.done{{background:#1d3a2a;color:#5bd68a}}
 .pill.now{{background:#3a2f1d;color:#f0b64c;box-shadow:0 0 0 1px #5a4a2a inset}}
 .ok{{color:#5bd68a}} .bad{{color:#f06a6a}} .muted{{color:#6b7382}}
 ul{{margin:0;padding-left:20px}} li{{margin:4px 0}}
 .note{{white-space:pre-wrap;line-height:1.8}}
</style></head><body><div class="wrap">
<h1>🐋 小鲸鱼</h1>
<div class="sub">{now} ｜ 每 30 秒自动刷新</div>

<div class="card"><h2>连接</h2>
{"<span class='ok'>● 在线</span>" if online else "<span class='bad'>● 掉线</span>"}
　｜　家里电脑：{home_txt}
</div>

<div class="card"><h2>她最近的状态</h2>
<div class="note">{note}</div></div>

<div class="card"><h2>今天的说说（{len(qdone)}/{len(qplan.get("hours") or [])} 条）</h2>
{hour_list(qplan.get("hours") or [], qdone)}
<div class="muted" style="margin-top:8px;font-size:13px">绿色=已发，黄色=当前这一小时</div></div>

<div class="card"><h2>今天申请看视频（{len(wdone)}/{len(wplan.get("hours") or [])} 次）</h2>
{hour_list(wplan.get("hours") or [], wdone)}</div>

<div class="card"><h2>最近发的说说</h2><ul>{recent}</ul></div>
</div></body></html>"""
    return web.Response(text=page, content_type="text/html")


def _check_home_token(request) -> bool:
    if not config.HOME_TOKEN:
        return False
    given = request.headers.get("X-Home-Token") or request.query.get("token", "")
    return given == config.HOME_TOKEN


async def _jobs_poll(request: web.Request) -> web.Response:
    """家里的电脑来问：有活干吗？没有就等 1 秒再回。"""
    if not _check_home_token(request):
        return web.json_response({"error": "forbidden"}, status=403)

    # 记一下对面中继的代码版本，好判断它是不是还跑着旧代码
    version = request.headers.get("X-Home-Version") or request.query.get("v") or ""
    try:
        HOME_VERSION["value"] = int(version)
        HOME_VERSION["ts"] = time.time()
    except (TypeError, ValueError):
        pass

    if JOBS is None:
        return web.json_response({"job": None})
    try:
        job = await asyncio.wait_for(JOBS.get(), timeout=1.0)
    except asyncio.TimeoutError:
        return web.json_response({"job": None})
    return web.json_response({"job": job})


async def _jobs_result(request: web.Request) -> web.Response:
    """家里的电脑交作业。"""
    if not _check_home_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "bad json"}, status=400)

    job_id = data.get("id")
    result = data.get("result")

    # 幂等：家里那边交作业失败会重试，同一条可能来两次，别重复投递
    delivered = request.app.setdefault("delivered", set())
    if job_id in delivered:
        log.info("作业 %s 已经处理过，忽略重复提交", job_id)
        return web.json_response({"ok": True, "dup": True})

    future = PENDING.get(job_id)
    if future is not None and not future.done():
        future.set_result(result)
        delivered.add(job_id)
        if len(delivered) > 500:
            delivered.clear()
        return web.json_response({"ok": True})

    # 没人认领：多半是服务重启过，等活的那个任务已经被清掉了。
    # 结果里要是有音频就别白扔，直接补发给主人（踩过：重启一次丢一首歌）
    bot: WhaleBot = request.app["bot"]
    if isinstance(result, dict) and result.get("audio"):
        log.info("收到一份没人认领的成品，直接补发")
        delivered.add(job_id)
        bot._spawn(bot._deliver_orphan_song(result))
    return web.json_response({"ok": True})


async def _songs_upload(request: web.Request) -> web.Response:
    """家里电脑批量做好的歌往曲库里塞。"""
    if not _check_home_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "bad json"}, status=400)

    query = str(data.get("query") or "").strip()
    title = str(data.get("title") or query).strip()
    audio = data.get("audio") or ""
    ext = str(data.get("ext") or "mp3")
    segment = data.get("segment")
    total = data.get("total")
    if not query or not audio:
        return web.json_response({"error": "缺 query 或 audio"}, status=400)
    try:
        raw = base64.b64decode(audio)
    except Exception:
        return web.json_response({"error": "audio 不是合法 base64"}, status=400)

    entry = songlib.add_segment(
        query,
        title,
        int(segment) if segment is not None else 0,
        raw,
        ext=ext,
        total=int(total) if total is not None else None,
    )
    log.info("曲库收到《%s》（%d KB，共 %d 首）", title[:30], len(raw) // 1024, songlib.count())
    return web.json_response({"ok": True, "file": entry.get("file"), "count": songlib.count()})


async def _songs_index(request: web.Request) -> web.Response:
    """给批量生成脚本看：库里已经有哪些，好跳过。"""
    if not _check_home_token(request):
        return web.json_response({"error": "forbidden"}, status=403)
    return web.json_response(
        {"ok": True, "count": songlib.count(), "songs": songlib.listing()}
    )


def build_app(bot: WhaleBot) -> web.Application:
    global JOBS
    JOBS = asyncio.Queue()

    # 默认上限只有 1MB，而一首 60 秒的翻唱 mp3 转成 base64 就有 1.2MB，
    # 家里电脑交作业时会直接 413。放开到 64MB（隧道里自己的数据，不怕）。
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["bot"] = bot
    app.router.add_post("/", bot.on_event)
    app.router.add_post("/onebot", bot.on_event)
    app.router.add_get("/", lambda r: web.json_response({"alive": True, "model": config.MODEL}))
    app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
    app.router.add_get("/status", _status_page)
    app.router.add_post("/songs/upload", _songs_upload)
    app.router.add_get("/songs/index", _songs_index)
    app.router.add_get("/jobs/poll", _jobs_poll)
    app.router.add_post("/jobs/result", _jobs_result)
    return app


async def _on_startup(app: web.Application) -> None:
    bot: WhaleBot = app["bot"]
    log.info("监听 http://%s:%s ｜ 模型 %s", config.HOST, config.PORT, config.MODEL)
    log.info("允许的 QQ：%s", "、".join(str(x) for x in config.OWNER_QQS) or "（无）")
    # 健康巡检：掉线/恢复都记日志，方便找规律
    bot._spawn(bot._watchdog())
    # 主动找人说话
    bot._spawn(bot._proactive_loop())
    # 没回就追一句
    bot._spawn(bot._followup_loop())
    # 每天申请去看视频
    bot._spawn(bot._watch_video_loop())
    # 记忆的日常维护：凌晨 4 点衰减+巩固+做梦，5 点跑影子对比
    bot._spawn(bot._neuron_maint_loop())
    bot._spawn(bot._remind_loop())
    bot._spawn(bot._neuron_shadow_loop())
    # 听邮箱：主人发邮件要二维码
    bot._spawn(bot._mail_trigger_loop())
    # 每天自己发几条说说
    bot._spawn(bot._qzone_loop())
    if config.MOCK_LLM:
        log.info("MOCK_LLM 已开启：不会真正调用模型")
        return
    try:
        info = await bot.ob.get_login_info()
        bot.me = info if isinstance(info, dict) else None
        log.info("NapCat 已连通，机器人账号：%s", (bot.me or {}).get("nickname", "未知"))
    except onebot.OneBotError as exc:
        log.warning("连不上 NapCat 的 HTTP API：%s", exc)
        log.warning("→ 检查 NapCat「网络配置」里 HTTP 服务器是否开启、端口是否等于 ONEBOT_API_BASE")


async def _on_cleanup(app: web.Application) -> None:
    await app["bot"].ob.close()
    await llm.close()
    await media.close()


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s ｜ %(message)s",
        datefmt="%H:%M:%S",
    )
    # 家里电脑每 1.5 秒来一次，访问日志会把有用的信息全冲掉
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    problems = config.check()
    if problems:
        print("启动前还差点东西：")
        for item in problems:
            print("  [!]", item)
        print("\n照着 .env.example 填好 .env 再跑一次。")
        return 1

    bot = WhaleBot()
    app = build_app(bot)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    try:
        web.run_app(app, host=config.HOST, port=config.PORT, print=None)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"端口 {config.PORT} 起不来（被占用？）：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
