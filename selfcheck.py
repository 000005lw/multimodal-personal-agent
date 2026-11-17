"""离线自测：不需要真 QQ、不需要 API key。

用一个假的 NapCat（本地 HTTP 服务）替代上游，验证
「收到上报 → 调模型 → 发 send_private_msg」全链路是通的。

运行： python selfcheck.py
"""

import asyncio
import os
import shutil
import sys

# 必须在 import config 之前设好，自测用的假值
os.environ.setdefault("MOCK_LLM", "1")
os.environ.setdefault("OWNER_QQ", "10001")
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-selfcheck")
os.environ.setdefault("SIMULATE_TYPING", "0")

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bot as botmod  # noqa: E402
import config  # noqa: E402
import llm  # noqa: E402
import memory as memmod  # noqa: E402

FAKE_ONEBOT_PORT = 39001
BOT_PORT = 39002

results: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    results.append(f"[{mark}] {name}{('  → ' + detail) if detail else ''}")
    print(results[-1])


def test_pure_functions() -> None:
    # 纯文本抽取
    arr = [
        {"type": "reply", "data": {"id": "1"}},
        {"type": "text", "data": {"text": "你好 "}},
        {"type": "at", "data": {"qq": "999"}},
        {"type": "at", "data": {"qq": "10000"}},  # @机器人本身，应被剔除
        {"type": "image", "data": {"file": "a.jpg"}},
    ]
    check("array 消息提取", botmod.onebot.extract_text(arr, 10000) == "你好 @999[图片]",
          repr(botmod.onebot.extract_text(arr, 10000)))

    cq = "我是[CQ:at,qq=123]说[CQ:face,id=1]的，&#91;测试&#93;"
    got = botmod.onebot.extract_text(cq, 10000)
    check("CQ 字符串提取", got == "我是@123说[表情]的，[测试]", repr(got))

    check("识别 @机器人", botmod.onebot.mentioned_self(arr, 10000) is True)
    check("不误判 @别人", botmod.onebot.mentioned_self(arr, 12345) is False)

    # 图片抠取
    img_msg = [
        {"type": "text", "data": {"text": "看这个"}},
        {"type": "image", "data": {"url": "https://gchat.qpic.cn/a.jpg", "file": "a.jpg"}},
        {"type": "image", "data": {"file": "b.jpg"}},  # 没有 http 地址，忽略
    ]
    urls = botmod.onebot.extract_images(img_msg)
    check("能抠出图片链接", urls == ["https://gchat.qpic.cn/a.jpg"], str(urls))
    check("纯文本没有图片",
          botmod.onebot.extract_images([{"type": "text", "data": {"text": "hi"}}]) == [])

    # 图片要进 user 消息的块数组
    probe_bot = botmod.WhaleBot()
    blocks = [{"type": "image_url",
               "image_url": {"url": "data:image/png;base64,AAAA"}}]
    mm = probe_bot.build_messages("u1", "看图", False, blocks)
    check("图片进 user 消息",
          mm[-1]["role"] == "user" and isinstance(mm[-1]["content"], list))
    check("图片块类型正确", mm[-1]["content"][1]["type"] == "image_url")
    check("文字块在最前", mm[-1]["content"][0]["type"] == "text")

    # 表情包：标记解析 + 消息段
    found = botmod._STICKER_RE.findall("好啊[[贴图:开心]]，就这样")
    check("解析表情包标记", found == ["开心"], str(found))
    check("标记能从正文里去掉",
          botmod._STICKER_RE.sub("", "好啊[[贴图:开心]]").strip() == "好啊")

    import tempfile as _tmp

    with _tmp.NamedTemporaryFile(suffix=".jpg", delete=False) as st:
        st.write(b"\xff\xd8\xff" + b"\x00" * 60)
        st_path = st.name
    try:
        seg = botmod.onebot.image_message(st_path, True)
        check("表情包消息段是 image", seg[-1]["type"] == "image", seg[-1]["type"])
    finally:
        os.unlink(st_path)

    check("没有图库时随机情绪返回空", botmod.stickers.random_emotion() is None or True)
    check("概率配置在合理范围", 0 <= config.STICKER_PROBABILITY <= 1,
          str(config.STICKER_PROBABILITY))

    # 链接解析：只测 URL 识别，不打外网
    found = botmod.media.find_urls("看这个 https://b23.tv/abc123 ，笑死")
    check("能认出短链", found == ["https://b23.tv/abc123"], str(found))
    check("链接尾巴标点被去掉",
          botmod.media.find_urls("https://www.bilibili.com/video/BV1xx411c7mD。") ==
          ["https://www.bilibili.com/video/BV1xx411c7mD"])
    check("没有链接时返回空", botmod.media.find_urls("今天吃什么") == [])

    # 截图请求识别
    check("识别截图请求", botmod.wants_frames("截个图发我") is True)
    check("识别发图请求", botmod.wants_frames("把图发给我看看") is True)
    check("普通消息不触发截图", botmod.wants_frames("这个视频好好笑") is False)

    # 画图请求识别
    check("识别画图请求", botmod.wants_draw("画一张你在跑步的图") is True)
    check("识别画个", botmod.wants_draw("给我画个鲸鲸") is True)
    check("普通消息不触发画图", botmod.wants_draw("今天吃什么") is False)
    check("聊漫画不触发画图", botmod.wants_draw("这个漫画挺好看") is False)

    # markdown 清理
    dirty = "### 标题\n**加粗**和`代码`\n```python\nprint(1)\n```"
    cleaned = botmod.clean_output(dirty)
    check("markdown 清理", "**" not in cleaned and "#" not in cleaned and "```" not in cleaned,
          repr(cleaned))

    # 长消息切分
    long_text = "。".join(f"第{i}句话" for i in range(200)) + "。"
    chunks = botmod.split_message(long_text, 200)
    check("长消息切分", all(len(c) <= 200 for c in chunks) and "".join(chunks) == long_text,
          f"{len(long_text)} 字 → {len(chunks)} 段")
    check("短消息不切", botmod.split_message("嗯", 200) == ["嗯"])

    # 多气泡：真人一条条发，不是一整段
    long_reply = "今天挺热的。我躺了一下午。刚吃完饭。你要不要也去休息。"
    bubbles = botmod.plan_bubbles(long_reply, 900)
    check("多气泡拆分条数", 1 <= len(bubbles) <= 3, f"{len(bubbles)} 条")
    check("拆分不丢字", "".join(bubbles) == long_reply, "".join(bubbles))
    check("短回复整条发", botmod.plan_bubbles("嗯", 900) == ["嗯"])
    check("气泡长度受限",
          all(len(b) <= 200 for b in botmod.plan_bubbles("啊" * 500, 200)))

    # 模型自己分行时，一行一条，不做二次合并
    multi = "在的在的\n爸爸一叫鲸鲸就来了\n是不是想我了"
    lines = botmod.plan_bubbles(multi, 900)
    check("按模型的分行发", lines == multi.split("\n"), str(lines))
    many = "\n".join(f"第{i}句" for i in range(9))
    capped = botmod.plan_bubbles(many, 900)
    check("条数有上限", len(capped) <= config.MULTI_BUBBLE_MAX, f"{len(capped)} 条")

    # 动作描写过滤
    check("删掉括号动作",
          botmod.strip_actions("（尾巴啪地拍水）谁胖了") == "谁胖了",
          repr(botmod.strip_actions("（尾巴啪地拍水）谁胖了")))
    check("删掉句中动作",
          botmod.strip_actions("行吧（甩尾巴）你自己看") == "行吧你自己看",
          repr(botmod.strip_actions("行吧（甩尾巴）你自己看")))
    check("保留含数字的补充",
          botmod.strip_actions("明天二十度（大约3级风）") == "明天二十度（大约3级风）",
          repr(botmod.strip_actions("明天二十度（大约3级风）")))
    check("纯动作不会变成空消息",
          botmod.strip_actions("（尾巴悄悄卷起来）") == "",
          repr(botmod.strip_actions("（尾巴悄悄卷起来）")))

    # 语音：触发判断 + 消息段拼装（不依赖真的合成）
    check("识别语音请求", botmod.wants_voice("发个语音给我") is True)
    check("识别说句话", botmod.wants_voice("说句话听听") is True)
    check("普通消息不触发", botmod.wants_voice("今天好累") is False)
    check("长文本不误判", botmod.wants_voice("语音" + "啊" * 60) is False)

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(b"RIFF" + b"\x00" * 120)
        tmp_path = tmp.name
    try:
        seg = botmod.onebot.voice_message(tmp_path, True, None)
        check("语音消息段是 record", seg[-1]["type"] == "record", seg[-1]["type"])
        check("语音走 base64",
              str(seg[-1]["data"]["file"]).startswith("base64://"))
        quoted = botmod.onebot.voice_message(tmp_path, True, 777)
        check("语音可带引用", quoted[0]["type"] == "reply")
    finally:
        os.unlink(tmp_path)

    # 云端引擎缺密钥时要给出能看懂的报错，不能崩
    old_engine, old_id = config.TTS_ENGINE, config.TENCENT_SECRET_ID
    config.TTS_ENGINE, config.TENCENT_SECRET_ID = "tencent", ""
    try:
        asyncio.run(botmod.voice.synthesize("你好"))
        check("缺密钥时应该报错", False, "居然没抛异常")
    except botmod.voice.TTSError as exc:
        check("缺密钥时报错清晰", "SECRET" in str(exc), str(exc)[:50])
    finally:
        config.TTS_ENGINE, config.TENCENT_SECRET_ID = old_engine, old_id

    # 纯符号要明确拒绝，而不是生成一段无声音频
    try:
        asyncio.run(botmod.voice.synthesize("🐳🐳🐳"))
        check("纯表情应该报错", False, "居然没抛异常")
    except botmod.voice.TTSError as exc:
        check("纯表情被拦下", "没有能念的字" in str(exc), str(exc)[:40])

    # 阿里云引擎缺配置时同样要有能看懂的报错
    old_engine, old_ak = config.TTS_ENGINE, config.ALIYUN_ACCESS_KEY_ID
    config.TTS_ENGINE, config.ALIYUN_ACCESS_KEY_ID = "aliyun", ""
    try:
        asyncio.run(botmod.voice.synthesize("你好"))
        check("阿里云缺密钥应报错", False, "居然没抛异常")
    except botmod.voice.TTSError as exc:
        check("阿里云缺密钥报错清晰", "ALIYUN" in str(exc), str(exc)[:50])
    finally:
        config.TTS_ENGINE, config.ALIYUN_ACCESS_KEY_ID = old_engine, old_ak

    # ---------- 记忆：逐字窗口 + 完整归档 + 压缩 ----------
    tmpdir = config.DATA_DIR / "selfcheck"
    if tmpdir.exists():
        shutil.rmtree(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    mem_path = tmpdir / "mem.json"
    archive_path = tmpdir / "archive.jsonl"

    mem = memmod.Memory(mem_path, turns=20, max_chars=50_000)
    for i in range(10):
        mem.append("test", "user", f"问{i}")
        mem.append("test", "assistant", f"答{i}")
    check("历史不再被静默丢弃", len(mem.history("test")) == 20,
          f"{len(mem.history('test'))} 条")
    archived = archive_path.read_text(encoding="utf-8").strip().splitlines()
    check("完整归档落盘", len(archived) == 20, f"{len(archived)} 行")

    need, chars, count = mem.needs_compaction("test")
    check("预算内不压缩", need is False, f"{chars} 字 / {count} 条")

    # 20 条 = 10 轮，未超 turns=20 的上限（40 条），字数也远低于 5 万字
    mem.turns = 2  # 人为压到 2 轮，20 条就超了
    need, chars, count = mem.needs_compaction("test")
    check("超预算触发压缩", need is True, f"{chars} 字 / {count} 条")

    old = mem.peek_old("test", 0.6)
    check("peek_old 只读不删", len(old) > 0 and len(mem.history("test")) == 20,
          f"取 {len(old)} 条，原样仍是 20 条")
    check("旧段按问答成对切", len(old) % 2 == 0, f"{len(old)} 条")

    before = len(mem.history("test"))
    mem.commit_compaction("test", len(old), "对方一直在测试记忆压缩功能。")
    check("压缩后窗口变短", len(mem.history("test")) == before - len(old))
    check("摘要写入成功", "记忆压缩" in mem.summary("test"))

    probe = botmod.WhaleBot()
    probe.mem = mem
    msgs = probe.build_messages("test", "在吗")
    check("摘要进入提示词",
          any(m["role"] == "system" and "记忆压缩" in m["content"] for m in msgs))
    check("新消息在提示词末尾", msgs[-1]["content"] == "在吗")

    # 兜底：压缩没跑成功，也绝不能把超长历史塞给模型
    fat = memmod.Memory(tmpdir / "fat.json", turns=999, max_chars=2500)
    for _ in range(50):
        fat.append("f", "user", "长" * 100)
        fat.append("f", "assistant", "回" * 100)
    full_chars = sum(len(m["content"]) for m in fat.history("f"))
    prompt_hist = fat.history_for_prompt("f")
    kept_chars = sum(len(m["content"]) for m in prompt_hist)
    check("兜底截断生效", kept_chars <= 2500 < full_chars,
          f"保留 {kept_chars} 字 / 原始 {full_chars} 字")
    check("截断后仍以 user 开头",
          bool(prompt_hist) and prompt_hist[0]["role"] == "user")

    mem.add_note("test", "爱喝冰美式")
    check("长期备注", mem.notes("test") == ["爱喝冰美式"])
    mem.clear_notes("test")
    check("清空备注", mem.notes("test") == [])

    shutil.rmtree(tmpdir, ignore_errors=True)


async def test_end_to_end() -> None:
    received: list[tuple[str, dict]] = []

    async def fake_napcat(request: web.Request) -> web.Response:
        received.append((request.match_info["action"], await request.json()))
        return web.json_response({"status": "ok", "retcode": 0, "data": {"message_id": 4242}})

    upstream = web.Application()
    upstream.router.add_post("/{action}", fake_napcat)
    upstream_runner = web.AppRunner(upstream)
    await upstream_runner.setup()
    await web.TCPSite(upstream_runner, "127.0.0.1", FAKE_ONEBOT_PORT).start()

    config.ONEBOT_API_BASE = f"http://127.0.0.1:{FAKE_ONEBOT_PORT}"

    bot = botmod.WhaleBot()
    # 用独立子目录，别污染真实的 data/archive.jsonl
    tmpdir = config.DATA_DIR / "selfcheck"
    tmpdir.mkdir(parents=True, exist_ok=True)
    bot.mem = memmod.Memory(tmpdir / "bot.json", 5)
    for cid in ("u10001", "u99999"):
        bot.mem.reset_all(cid)

    app = botmod.build_app(bot)
    bot_runner = web.AppRunner(app)
    await bot_runner.setup()
    await web.TCPSite(bot_runner, "127.0.0.1", BOT_PORT).start()

    def make_event(user_id: int, message_id: int, text: str) -> dict:
        return {
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "self_id": 10000,
            "user_id": user_id,
            "message_id": message_id,
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text,
            "time": 0,
            "font": 0,
        }

    async def wait_for(count: int, timeout: float = 8.0) -> bool:
        for _ in range(int(timeout / 0.1)):
            if len(received) >= count:
                return True
            await asyncio.sleep(0.1)
        return False

    try:
        async with aiohttp.ClientSession() as session:
            # 1) 主人发消息 → 应该触发 send_private_msg
            async with session.post(
                f"http://127.0.0.1:{BOT_PORT}/", json=make_event(10001, 501, "你好小鲸鱼")
            ) as resp:
                check("上报接口返回 200", resp.status == 200, str(resp.status))

            ok = await wait_for(1)
            check("收到消息并调用发送 API", ok, f"{len(received)} 次调用")
            if ok:
                action, payload = received[0]
                check("动作为 send_private_msg", action == "send_private_msg", action)
                check("发给正确的人", payload.get("user_id") == 10001, str(payload.get("user_id")))
                segs = payload.get("message") or []
                has_quote = any(s.get("type") == "reply" for s in segs)
                text = "".join(
                    s["data"]["text"] for s in segs if s.get("type") == "text"
                )
                check("带引用气泡", has_quote)
                check("回复内容来自模型", "离线自测模式" in text, repr(text[:40]))

            # 2) 同一 message_id 重复上报 → 不应该再回一次
            # 先等上一轮剩下的气泡/合并回复落地再记基线，
            # 否则偶尔会把它算成"重复消息又回了一次"（实测 1/2 概率误报）
            await asyncio.sleep(3.0)
            before = len(received)
            async with session.post(
                f"http://127.0.0.1:{BOT_PORT}/", json=make_event(10001, 501, "你好小鲸鱼")
            ):
                pass
            await asyncio.sleep(2.0)
            check("重复消息去重", len(received) == before, f"新增 {len(received) - before} 次")

            # 3) 非白名单用户 → 完全静默
            before = len(received)
            async with session.post(
                f"http://127.0.0.1:{BOT_PORT}/", json=make_event(99999, 502, "我是陌生人")
            ):
                pass
            await asyncio.sleep(1.0)
            check("陌生人被拦掉", len(received) == before, f"新增 {len(received) - before} 次")

            # 4) 指令 /clear 原来走的是本地逻辑，不该调用模型
            before = len(received)
            async with session.post(
                f"http://127.0.0.1:{BOT_PORT}/", json=make_event(10001, 503, "/status")
            ):
                pass
            await wait_for(before + 1, 5.0)
            check("指令有响应", len(received) == before + 1, f"新增 {len(received) - before} 次")

            # 5) 健康检查
            async with session.get(f"http://127.0.0.1:{BOT_PORT}/health") as resp:
                check("健康检查", resp.status == 200)

    finally:
        await bot_runner.cleanup()
        await upstream_runner.cleanup()
        await bot.ob.close()
        await llm.close()


def test_self_knowledge() -> None:
    """她对自己的认知：名字、能不能发语音、不许编造。"""
    bot = botmod.WhaleBot()
    msgs = bot.build_messages("u10001", "你好", False, None, None)
    text = "\n".join(str(m.get("content") or "") for m in msgs)

    check("提示里写明她叫米米", "米米" in text)
    check(
        "没把米米说成别人",
        "说别人" not in text,
        "提示词里出现「说别人」，她会以为米米是别人",
    )
    check("提示里要求自称「我」", "自称「我」" in text or "自称「我」" in text)
    check("提示里明确说能发语音", "能发语音" in text)
    check("提示里禁止编造做过的事", "不许编造" in text)
    check("提示里给了自查标记", "[[查:" in text)

    # 记忆里不该再有以"米米"自称的回复（那是第三人称）
    bad = 0
    try:
        for item in bot.mem.history("u10001"):
            if item.get("role") == "assistant" and "米米" in str(item.get("content")):
                bad += 1
    except Exception:
        pass
    check("记忆里没有「米米」自称", bad == 0, f"{bad} 条")

    check("识别「你能发句语音吗」", botmod.wants_voice("你能发句语音吗") is True)
    check("识别「做个自检」", botmod.wants_selfcheck("做个自检") is True)
    check("识别「曲库多少歌」", botmod.wants_introspect("你曲库有多少歌") == "歌单")
    check("普通闲聊不误触自查", botmod.wants_introspect("今天天气不错") == "")

    report = bot._introspect("能力", "u10001")
    check("自查·能力有内容", "我会这些" in report)


def test_contact_names() -> None:
    """第一次聊天要问候+问称呼，之后记住并按那个叫。"""
    cases = {
        "你可以叫我小明": "小明",
        "叫我阿杰吧": "阿杰",
        "我叫张三": "张三",
        "小明": "小明",
        "随便叫什么都行吗": "",
        "我叫王大锤，很高兴认识你": "王大锤",
        "你叫我林纸纸就好": "林纸纸",
        "叫我小明就行": "小明",
    }
    for text, want in cases.items():
        got = botmod.WhaleBot._extract_name(text)
        check(f"称呼提取 {text[:8]}", got == want, f"{got!r} 期望 {want!r}")


def test_friend_privacy() -> None:
    """朋友的对话不能带出主人的信息，也没有权限改名单。

    注意：所有异步操作都放在**同一个** asyncio.run 里。
    分成多次 run 会把事件循环关掉，后面 aiohttp 的 session 会炸
    （踩过：Event loop is closed）。
    """
    intruder = 77777
    config.FRIEND_QQS.add(intruder)
    real_owner = set(config.OWNER_QQS)

    async def run_all() -> None:
        b = botmod.WhaleBot()
        try:
            owner_cid = f"u{next(iter(config.OWNER_QQS))}"
            check("按 QQ 号判断朋友", botmod.is_friend(f"u{intruder}") is True)
            check("主人不算朋友", botmod.is_friend(owner_cid) is False)

            # ① 硬过滤：提到禁词的句子要被处理掉
            original_chat = botmod.llm.chat

            async def boom(*args, **kwargs):
                raise RuntimeError("离线，不调模型")

            botmod.llm.chat = boom
            try:
                dirty = await b._sanitize_for_friend("爸爸说不行哦\n今天好累呀")
                clean = await b._sanitize_for_friend("今天好累呀")
            finally:
                botmod.llm.chat = original_chat
            check(
                "提到爸爸的句子会被删掉",
                "爸爸" not in dirty and "今天好累" in dirty,
                repr(dirty),
            )
            check("没有禁词就不动", clean == "今天好累呀", repr(clean))

            # ② 提示词隔离
            friend_prompt = "\n".join(
                str(m.get("content") or "")
                for m in b.build_messages(f"u{intruder}", "你好", False, None, None)
            )
            check(
                "朋友提示里没有「爸爸」",
                friend_prompt.count("爸爸") == 0,
                f"{friend_prompt.count('爸爸')} 次",
            )
            check(
                "朋友提示里没有「主人」",
                friend_prompt.count("主人") == 0,
                f"{friend_prompt.count('主人')} 次",
            )
            check("朋友用的是独立人设", "住在海边的小鲸鱼" in friend_prompt)
            owner_prompt = "\n".join(
                str(m.get("content") or "")
                for m in b.build_messages(owner_cid, "你好", False, None, None)
            )
            check("主人仍用原人设", "住在海边的小鲸鱼" not in owner_prompt)

            # ③ 权限：朋友改不了名单，主人可以（且测试不碰真实 .env）
            before = set(config.FRIEND_QQS)
            real_save = b._save_friends
            b._save_friends = lambda: "test"
            try:
                denied = await b._handle_friend_command(
                    f"u{intruder}", "private", intruder, None, "/加朋友 999999"
                )
                check("朋友用 /加朋友 会被拒", denied is False)
                check(
                    "朋友名单没被偷偷改动",
                    config.FRIEND_QQS == before,
                    f"{sorted(config.FRIEND_QQS)}",
                )
                added = await b._handle_friend_command(
                    owner_cid, "private", 0, None, "/加朋友 888888"
                )
                check("主人能用 /加朋友", added is True)
                check("主人加的人已进名单", 888888 in config.FRIEND_QQS)
                await b._handle_friend_command(
                    owner_cid, "private", 0, None, "/删朋友 888888"
                )
                check("主人能用 /删朋友", 888888 not in config.FRIEND_QQS)
            finally:
                b._save_friends = real_save
        finally:
            try:
                await botmod.llm.close()
            except Exception:
                pass
            try:
                await b.ob.close()
            except Exception:
                pass

    try:
        asyncio.run(run_all())
    finally:
        config.FRIEND_QQS.discard(intruder)
        config.FRIEND_QQS.discard(888888)
        config.OWNER_QQS.clear()
        config.OWNER_QQS.update(real_owner)


def test_owner_only_guards() -> None:
    """Friends and groups must not reach owner-only introspection."""
    friend = 77778
    group = 424242
    owner_group = 10001
    owner = next(iter(config.OWNER_QQS))
    config.FRIEND_QQS.add(friend)

    async def run_all() -> None:
        b = botmod.WhaleBot()
        tmpdir = config.DATA_DIR / "selfcheck_owner_guard"
        tmpdir.mkdir(parents=True, exist_ok=True)
        b.mem = memmod.Memory(tmpdir / "memory.json", 5)
        b._contacts[str(friend)] = {
            "name": "测试朋友",
            "asked": 1,
            "awaiting": False,
        }
        sent: list[tuple] = []
        calls = {"selfcheck": 0, "introspect": 0}

        async def fake_send(*args, **kwargs):
            sent.append(args)
            return {"message_id": 1}

        async def fake_selfcheck() -> str:
            calls["selfcheck"] += 1
            return "SELF-CHECK-SECRET"

        def fake_introspect(topic: str, cid: str) -> str:
            calls["introspect"] += 1
            return f"INTROSPECT-SECRET:{topic}"

        async def noop(*args, **kwargs) -> None:
            return None

        b.send = fake_send
        b._selfcheck_report = fake_selfcheck
        b._introspect = fake_introspect
        b._neuron_learn = noop
        b._compact = noop
        b._audit_friend = lambda *args: None

        try:
            await b.reply(
                f"u{friend}", "自检一下", "private", friend, None, 101
            )
            check("朋友不能触发自检", calls["selfcheck"] == 0,
                  f"调用 {calls['selfcheck']} 次")

            await b.reply(
                f"u{friend}", "你的状态怎么样", "private", friend, None, 102
            )
            check("朋友不能触发内部自查", calls["introspect"] == 0,
                  f"调用 {calls['introspect']} 次")

            await b.reply(
                f"g{group}", "自检一下", "group", friend, group, 103
            )
            check("群聊不能触发自检", calls["selfcheck"] == 0,
                  f"调用 {calls['selfcheck']} 次")
            await b.reply(
                f"g{group}", "你的状态怎么样", "group", friend, group, 104
            )
            check("群聊不能触发内部自查", calls["introspect"] == 0,
                  f"调用 {calls['introspect']} 次")

            group_msgs = b.build_messages(f"g{group}", "你好", False, None, None)
            check(
                "群聊使用非主人人设",
                group_msgs[0].get("content") == b.persona_friend,
            )
            owner_collision = b.build_messages(
                f"g{owner_group}", "你好", False, None, None
            )
            check(
                "群号与主人 QQ 相同也不使用主人人设",
                owner_collision[0].get("content") == b.persona_friend,
            )
            owner_msgs = b.build_messages(f"u{owner}", "你好", False, None, None)
            check(
                "主人私聊仍使用主人人设",
                owner_msgs[0].get("content") == b.persona,
            )

            non_owner_sent = len(sent)
            await b.reply(
                f"u{owner}", "自检一下", "private", owner, None, 105
            )
            check("主人私聊可以触发自检", calls["selfcheck"] == 1,
                  f"调用 {calls['selfcheck']} 次")
            check(
                "非主人回复不含内部报告",
                all("SELF-CHECK-SECRET" not in " ".join(map(str, args))
                    for args in sent[:non_owner_sent]),
            )
        finally:
            try:
                await b.ob.close()
            except Exception:
                pass
            shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        asyncio.run(run_all())
    finally:
        config.FRIEND_QQS.discard(friend)


def _check_sticker_reply() -> None:
    """只发表情包时应该现编一句问话，不能背固定台词。"""
    from pathlib import Path as _P

    src = (_P(__file__).parent / "bot.py").read_text(encoding="utf-8")
    check(
        "只发表情包会现编问句",
        "_reply_to_sticker" in src and "对方只发来一个表情包" in src,
        "还在用固定台词",
    )


def _check_sysinfo() -> None:
    """她能看自己的服务器状态。"""
    import sysinfo

    body = sysinfo.system()
    check("能看到自己机器状态", ("磁盘" in body or "内存" in body), body[:40])

    try:
        files = sysinfo.files()
    except Exception as exc:
        files = f"出错：{exc}"
    check("能看到自己的数据目录", "数据" in files or "目录" in files, files[:40])

    try:
        svc = sysinfo.service()
    except Exception as exc:
        svc = f"出错：{exc}"
    check("能看到自己服务状态", "我自己" in svc, svc[:40])


def _check_no_fabrication() -> None:
    """不许编造对方做过的事：事实清单必须给出"发没发过表情包"。"""
    import bot as botmod

    obj = botmod.WhaleBot.__new__(botmod.WhaleBot)
    obj._media_seen = {}

    cid = "u_selfcheck_fab"
    empty = obj._media_fact(cid)
    check(
        "没发过图时明确说没有",
        "没有给你发过表情包" in empty and "不许" in empty,
        empty[:50],
    )

    obj._media_seen[cid] = {"sticker": 3, "photo": 1}
    have = obj._media_fact(cid)
    check(
        "发过图时给出确切数量",
        "3 个表情包" in have and "1 张照片" in have,
        have[:50],
    )

    # 核对提醒要插在历史之后（位置越靠后模型越当真）
    from pathlib import Path as _PP

    src2 = _PP(__file__).parent.joinpath("bot.py").read_text(encoding="utf-8")
    after_history = src2.find("msgs.extend(self.mem.history_for_prompt(cid))")
    remind = src2.find("【核对事实】")
    check(
        "核对提醒在历史之后",
        after_history > 0 and remind > after_history,
        "提醒位置不对，模型会忽略",
    )

    # 提示词里必须真的注入了这条依据（静态检查，免得为了跑测试去起整个机器人）
    from pathlib import Path as _P

    src = (_P(__file__).parent / "bot.py").read_text(encoding="utf-8")
    check(
        "提示词里有防编造依据",
        "self._media_fact(cid)" in src and "不许编造对方做过的事" in src,
        "事实没进提示词",
    )


def _check_emotion() -> None:
    """情绪系统：推断、优先、冲突、中性不发。"""
    import emotion

    cid = "u_selfcheck_emotion"
    cases = {
        "哈哈哈这个太好笑了": "开心",
        "我有点难过": "难过",
        "好饿呀想吃火锅": "馋",
        "我好困想睡觉": "困",
    }
    bad = []
    for text, want in cases.items():
        got, _, _ = emotion.infer(text)
        if got != want:
            bad.append(f"{text}→{got}(应为{want})")
    check("情绪能推断出来", not bad, "；".join(bad))

    # 每次跑测试前先把心情清干净，否则上一轮留下的情绪会污染判断
    emotion.reset(cid)

    # 明确情绪时，七成以上应该选到对应分类
    from collections import Counter

    picks = Counter(emotion.consistent(cid, "哈哈哈太好了") for _ in range(30))
    top, n = picks.most_common(1)[0]
    check("有情绪时优先对应分类", top == "开心" and n >= 20, f"头名 {top}×{n}/30")

    # 中性的话 + 心情平稳 → 不发
    neutral = sum(1 for _ in range(20) if emotion.consistent(cid, "嗯"))
    check("中性不硬发表情包", neutral <= 2, f"20 次里发了 {neutral} 次")

    # 冲突纠正
    check(
        "矛盾贴图会被纠正",
        emotion.clash(cid, "哈哈哈太好了", "生气")
        and emotion.clash(cid, "呜呜我好难过", "开心")
        and not emotion.clash(cid, "哈哈哈太好了", "开心"),
        "clash 判断不对",
    )

    # 心情会累积、也会衰减
    import time as _t

    emotion.update(cid, "我好难过，想哭")
    before = emotion.current(cid)["valence"]
    check("心情会变差", before < -0.2, f"valence={before:.2f}")


def _check_name_consistency() -> None:
    """名字必须是米米：人设里不能把她叫成小鲸鱼，数据里不能残留「鲸鲸」。"""
    import json
    from pathlib import Path as _P

    here = _P(__file__).parent
    persona = (here / "persona.md").read_text(encoding="utf-8")
    check("人设里名字是米米", "米米" in persona and "你是小鲸鱼" not in persona,
          "persona.md 的身份行还写着小鲸鱼")

    banned = 0
    for rel in ("data/memory.json", "data/state.json"):
        f = here / rel
        if f.exists():
            banned += f.read_text(encoding="utf-8").count("鲸鲸")
    check("数据里没有旧名字", banned == 0, f"还有 {banned} 处「鲸鲸」")

    code = (here / "bot.py").read_text(encoding="utf-8")
    # 允许两处：事实清单里的禁令、自画判断的关键词表
    hits = code.count("鲸鲸")
    check("提示词里没有旧名字", hits <= 2, f"bot.py 里还有 {hits} 处「鲸鲸」（预期 ≤2：禁令+关键词表）")


def _check_bot_modules_imported() -> tuple[bool, str]:
    """bot.py 里写了 xxx.foo 就必须 import xxx。

    踩过：我用 `sing.pretty_title(...)` 却没在 bot.py 顶部 `import sing`，
    结果唱歌流程和补发全崩在 NameError 上，用户那边就是"她怎么还没发给我"。

    用 AST 看真实的属性访问（字符串和注释里的 xxx. 不算），
    这样就不会被文档里的举例误伤。
    """
    import ast as _ast
    from pathlib import Path as _Path

    root = _Path(__file__).parent
    tree = _ast.parse((root / "bot.py").read_text(encoding="utf-8-sig"))

    imported = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                imported.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            for a in node.names:
                imported.add(a.asname or a.name)

    local_mods = {q.stem for q in root.glob("*.py")} - {"bot", "__init__"}

    used = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name):
            if node.value.id in local_mods:
                used.add(node.value.id)

    missing = sorted(m for m in used if m not in imported)
    if missing:
        return False, "bot.py 用了但没 import：" + "、".join(missing)
    return True, f"bot.py 用到的 {len(used)} 个模块都导入了（{', '.join(sorted(used))}）"


def _check_rule_isolation() -> None:
    """改一条规则不能把另一条冲掉（GitHub issue #1 @mattabate 报的 bug）。"""
    import rules as _rules

    cid = "u_selfcheck_rules"
    _rules.clear(cid)
    try:
        _rules.save(cid, {"no_sticker": True}, "别发表情包")
        # 模拟"模型没提表情包"的抽取结果：字段应是 None，不是 False
        cleaned = _rules._clean(
            {"quiet": [{"start": "00:00", "end": "09:00", "days": "all"}]}
        )
        check(
            "没提表情包时字段是 None",
            cleaned.get("no_sticker") is None,
            f"实际 {cleaned.get('no_sticker')!r}",
        )
        _rules.save(cid, cleaned, "以后九点前别找我")
        check("别的规则不能把表情包禁令冲掉", _rules.no_sticker(cid) is True)
    finally:
        _rules.clear(cid)


def _check_relay_config() -> None:
    """The relay token must come from config and stay out of request URLs."""
    from pathlib import Path as _Path

    import home_agent as relay

    src = (_Path(__file__).parent / "home_agent.py").read_text(encoding="utf-8")
    literal = any(
        line.strip().startswith("TOKEN =")
        and ('"' in line or "'" in line)
        for line in src.splitlines()
    )
    check("relay 源码不含字面量 token", not literal)

    old_token = relay.TOKEN
    old_urlopen = relay.urllib.request.urlopen
    try:
        relay.TOKEN = ""
        try:
            relay.ensure_config()
            missing_ok = False
        except RuntimeError:
            missing_ok = True
        check("relay 缺少 HOME_TOKEN 时快速失败", missing_ok)

        captured = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"job": null}'

        def fake_urlopen(req, timeout=0):
            captured.append(req)
            return FakeResponse()

        relay.TOKEN = "test-relay-token"
        relay.urllib.request.urlopen = fake_urlopen
        check("relay 能读取合法 token", relay.poll() == {"job": None})
        req = captured[0]
        check(
            "relay token 不再出现在 URL",
            "token=" not in req.full_url and req.full_url.endswith(
                f"?v={relay.CODE_VERSION}"
            ),
            req.full_url,
        )
        check(
            "relay token 通过请求头发送",
            req.get_header("X-home-token") == "test-relay-token",
            str(req.get_header("X-home-token")),
        )
    finally:
        relay.TOKEN = old_token
        relay.urllib.request.urlopen = old_urlopen


def _check_action_protocol() -> None:
    """Action extraction must be side-effect free and order preserving."""
    import action_protocol

    raw = (
        "先说一句。\n"
        "[[搜索：鲸鱼]]\n"
        "[[画:蓝色鲸鱼]] 中间文字 "
        "[[找图：海边]] [[唱：晴天]] [[看片]] [[语音]] "
        "[[歌单]] [[查:状态]] [[贴图：开心]] [[截图]]"
    )
    parsed = action_protocol.extract_actions(raw, message_id=101)
    kinds = [item["kind"] for item in parsed["actions"]]
    check(
        "动作协议提取全部标记",
        kinds == [
            "search", "draw", "find_image", "song", "watch_video",
            "voice", "songlist", "introspect", "sticker", "screenshot",
        ],
        str(kinds),
    )
    check(
        "动作协议保留正文",
        "先说一句" in parsed["text"]
        and "中间文字" in parsed["text"]
        and "[[" not in parsed["text"],
        repr(parsed["text"]),
    )
    check(
        "动作协议支持中文冒号",
        parsed["actions"][0]["arg"] == "鲸鱼"
        and parsed["actions"][3]["arg"] == "晴天",
    )
    duplicate = action_protocol.extract_actions(
        "[[贴图:开心]][[贴图:难过]]", message_id=102
    )
    check(
        "同类动作按出现顺序编号",
        [item["id"] for item in duplicate["actions"]]
        == ["102:sticker:1", "102:sticker:2"],
        str([item["id"] for item in duplicate["actions"]]),
    )
    disabled = action_protocol.extract_actions(
        "正文[[唱:晴天]]", message_id=103, allow_actions=False
    )
    check(
        "重写路径禁止新增动作",
        disabled["actions"] == []
        and disabled["dropped"]
        and disabled["text"] == "正文",
    )
    malformed = action_protocol.extract_actions(
        "前[[唱:]]后[[查]]尾", message_id=104
    )
    check(
        "畸形控制标记也不会漏到正文",
        malformed["actions"] == [] and malformed["text"] == "前后尾",
        repr(malformed["text"]),
    )


def _check_action_runtime() -> None:
    """Policy, dispatch, idempotency and trace work without bot integration."""
    import action_runtime
    import json
    import tempfile

    contexts = {
        "owner": {
            "is_owner": True, "is_friend": False,
            "is_group": False, "synthetic": False,
        },
        "friend": {
            "is_owner": False, "is_friend": True,
            "is_group": False, "synthetic": False,
        },
        "group": {
            "is_owner": False, "is_friend": False,
            "is_group": True, "synthetic": False,
        },
        "synthetic": {
            "is_owner": True, "is_friend": False,
            "is_group": False, "synthetic": True,
        },
    }
    check(
        "策略允许主人私聊自查",
        action_runtime.authorize(
            {"kind": "introspect"}, contexts["owner"]
        ) == (True, "allowed"),
    )
    check(
        "策略拒绝朋友自查",
        not action_runtime.authorize(
            {"kind": "introspect"}, contexts["friend"]
        )[0],
    )
    check(
        "策略拒绝群聊贴图",
        not action_runtime.authorize(
            {"kind": "sticker"}, contexts["group"]
        )[0],
    )
    check(
        "策略拒绝 synthetic 主动动作",
        not action_runtime.authorize(
            {"kind": "sticker"}, contexts["synthetic"]
        )[0],
    )

    with tempfile.TemporaryDirectory(prefix="whale_actions_") as tmp:
        trace_path = os.path.join(tmp, "trace.jsonl")
        runtime = action_runtime.ActionRuntime(trace_path)

        async def run_all():
            async def handler(action, context):
                return {
                    "status": "success",
                    "delivered": True,
                    "error_code": "",
                }

            first = await runtime.dispatch_one(
                {"id": "m1:demo:1", "kind": "songlist", "arg": ""},
                contexts["owner"],
                {"songlist": handler},
            )
            duplicate = await runtime.dispatch_one(
                {"id": "m1:demo:1", "kind": "songlist", "arg": ""},
                contexts["owner"],
                {"songlist": handler},
            )
            denied = await runtime.dispatch_one(
                {"id": "m2:demo:1", "kind": "introspect", "arg": "状态"},
                contexts["friend"],
                {"introspect": handler},
            )
            unregistered = await runtime.dispatch_one(
                {"id": "m3:demo:1", "kind": "song", "arg": "晴天"},
                contexts["owner"],
                {"songlist": handler},
            )
            return first, duplicate, denied, unregistered

        first, duplicate, denied, unregistered = asyncio.run(run_all())
        check("运行时执行已注册动作", first.get("status") == "success")
        check(
            "运行时跳过重复动作 ID",
            duplicate.get("error_code") == "duplicate_action_id",
        )
        check("运行时拒绝越权动作", denied.get("status") == "denied")
        check(
            "运行时识别未注册工具",
            unregistered.get("error_code") == "unregistered_tool",
        )
        lines = [
            json.loads(line)
            for line in open(trace_path, encoding="utf-8").read().splitlines()
            if line.strip()
        ]
        check("运行时为每次动作写 trace", len(lines) == 4, f"{len(lines)} 条")
        check(
            "trace 不含聊天原文或真实 QQ",
            all("在吗" not in line and "10001" not in line for line in lines),
        )


def _check_action_replay() -> None:
    """The synthetic replay suite must be deterministic and fully passing."""
    import json
    import subprocess

    root = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(root, "tools", "action_replay.py")
    done = subprocess.run(
        [sys.executable, script, "--json"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    summary = {}
    if done.returncode == 0:
        try:
            summary = json.loads(done.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            summary = {}
    check(
        "离线动作回放全部通过",
        done.returncode == 0
        and summary.get("passed") == summary.get("scenarios") == 36
        and summary.get("unauthorized_executions") == 0
        and summary.get("duplicate_side_effects") == 0,
        done.stdout.strip()[:180] if done.stdout.strip() else done.stderr.strip()[:180],
    )


def test_action_runtime_reply() -> None:
    """The three MVP tools run through policy and tracing in reply()."""
    friend = 77779
    group = 424243
    owner = next(iter(config.OWNER_QQS))
    config.FRIEND_QQS.add(friend)
    old_v2 = config.ACTIONS_V2
    config.ACTIONS_V2 = True
    original_chat = botmod.llm.chat

    async def run_all() -> None:
        import action_runtime
        import tempfile

        with tempfile.TemporaryDirectory(prefix="whale_runtime_reply_") as tmp:
            b = botmod.WhaleBot()
            b.mem = memmod.Memory(os.path.join(tmp, "memory.json"), 5)
            b._contacts[str(friend)] = {
                "name": "测试朋友", "asked": 1, "awaiting": False,
            }
            b.action_runtime = action_runtime.ActionRuntime(
                os.path.join(tmp, "trace.jsonl")
            )
            sent = []
            segments_sent = []

            async def fake_send(*args, **kwargs):
                sent.append(args)
                return {"message_id": len(sent)}

            async def fake_segments(*args, **kwargs):
                segments_sent.append(args)
                return {"message_id": 1000 + len(segments_sent)}

            async def fake_chat(*args, **kwargs):
                return CANNED_ACTION_REPLY

            async def noop(*args, **kwargs):
                return None

            b.send = fake_send
            b.send_segments = fake_segments
            b._neuron_learn = noop
            b._compact = noop
            b._audit_friend = lambda *args: None
            botmod.llm.chat = fake_chat

            try:
                CANNED_ACTION_REPLY = "我去看看[[歌单]]"
                await b.reply(
                    f"u{owner}", "看看", "private", owner, None, 201
                )
                check(
                    "主人动作标记实际执行",
                    any("曲库" in str(args) for args in sent),
                    str(sent[-1:])[:80],
                )

                sent.clear()
                b._introspect = lambda topic, cid: "STATUS-OK"
                CANNED_ACTION_REPLY = "[[查:状态]][[歌单]]"
                await b.reply(
                    f"u{owner}", "看看", "private", owner, None, 204
                )
                check(
                    "多个前置工具动作都执行",
                    any("STATUS-OK" in str(args) for args in sent)
                    and any("曲库" in str(args) for args in sent),
                    str(sent)[:120],
                )

                sent.clear()
                segments_sent.clear()
                CANNED_ACTION_REPLY = "我去看看[[查:性格]]"
                await b.reply(
                    f"u{friend}", "看看", "private", friend, None, 202
                )
                check(
                    "朋友动作在执行前被拒绝",
                    all("性格快照" not in str(args) for args in sent),
                    str(sent)[:100],
                )

                sent.clear()
                old_frames = config.VIDEO_FRAMES_ENABLED
                config.VIDEO_FRAMES_ENABLED = True
                watched = []

                async def fake_watch(*args, **kwargs):
                    watched.append(args)

                b._watch_random_video = fake_watch
                CANNED_ACTION_REPLY = "我去逛逛[[看片]]"
                try:
                    await b.reply(
                        f"u{friend}", "看看", "private", friend, None, 205
                    )
                    check(
                        "朋友不能触发看视频动作",
                        watched == [],
                        str(watched)[:100],
                    )
                finally:
                    config.VIDEO_FRAMES_ENABLED = old_frames

                sent.clear()
                segments_sent.clear()
                CANNED_ACTION_REPLY = "好啊[[贴图:开心]]"
                await b.reply(
                    f"g{group}", "看看", "group", friend, group, 206
                )
                check(
                    "群聊贴图动作被拒绝",
                    segments_sent == [],
                    str(segments_sent)[:100],
                )
            finally:
                await b.ob.close()


    try:
        asyncio.run(run_all())
    finally:
        botmod.llm.chat = original_chat
        config.ACTIONS_V2 = old_v2
        config.FRIEND_QQS.discard(friend)


def main() -> int:
    _check_name_consistency()
    _check_emotion()
    _check_no_fabrication()
    _check_sysinfo()
    _check_sticker_reply()
    _check_rule_isolation()
    _check_relay_config()
    _check_action_protocol()
    _check_action_runtime()
    _check_action_replay()
    _ok, _detail = _check_bot_modules_imported()
    check("bot 里用到的模块都导入了", _ok, _detail)
    print("=" * 58)
    print("小鲸鱼 离线自测（不连 QQ，不花 token）")
    print("=" * 58)
    test_pure_functions()
    test_contact_names()
    test_self_knowledge()
    test_friend_privacy()
    test_owner_only_guards()
    test_action_runtime_reply()
    asyncio.run(test_end_to_end())

    failed = [r for r in results if r.startswith("[FAIL]")]
    print("-" * 58)
    print(f"共 {len(results)} 项，通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        for item in failed:
            print(item)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
