"""掉线邮件提醒。

只用标准库 smtplib，不额外装依赖。
QQ 邮箱要用「授权码」登录，不是 QQ 密码。
"""

import logging
import smtplib
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

from whale import config

log = logging.getLogger("whale.notify")


def send_mail(subject: str, body: str, image: Path | None = None) -> bool:
    """发一封邮件。image 给了就把图片当附件（用来发登录二维码）。

    返回是否成功，失败只记日志不抛——邮件发不出去不能连带影响别的功能。
    """
    if not config.MAIL_ENABLED:
        return False
    if not (config.MAIL_FROM and config.MAIL_AUTH_CODE and config.MAIL_TO):
        log.warning("邮件配置不全（发件人/授权码/收件人），跳过发送")
        return False

    has_image = image is not None and Path(image).exists()
    if has_image:
        message = MIMEMultipart()
        message.attach(MIMEText(body, "plain", "utf-8"))
        try:
            raw = Path(image).read_bytes()
            part = MIMEImage(raw, _subtype="png")
            part.add_header(
                "Content-Disposition", "attachment", filename="qrcode.png"
            )
            message.attach(part)
        except OSError as exc:
            log.warning("二维码附件读取失败：%s", exc)
            message = MIMEText(body, "plain", "utf-8")
    else:
        message = MIMEText(body, "plain", "utf-8")

    message["Subject"] = Header(subject, "utf-8")
    message["From"] = formataddr(("小鲸鱼", config.MAIL_FROM))
    message["To"] = config.MAIL_TO

    try:
        with smtplib.SMTP_SSL(
            config.MAIL_SMTP_HOST, config.MAIL_SMTP_PORT, timeout=20
        ) as smtp:
            smtp.login(config.MAIL_FROM, config.MAIL_AUTH_CODE)
            smtp.sendmail(config.MAIL_FROM, [config.MAIL_TO], message.as_string())
    except Exception as exc:  # 邮件发不出去不能连带影响别的功能
        log.warning("邮件发送失败：%s", exc)
        return False

    log.info("已发提醒邮件到 %s", config.MAIL_TO)
    return True


def notify_offline(when: str, reason: str) -> bool:
    subject = "小鲸鱼掉线了"
    body = (
        f"时间：{when}\n"
        f"原因：{reason}\n\n"
        "QQ 会话被踢了，需要重新扫码才能恢复。\n\n"
        "如果你在外面：下一封邮件会带登录二维码，但 QQ 不接受相册图片扫码，\n"
        "得找另一块屏幕（电脑/平板/别的手机）把二维码显示出来，再用手机摄像头扫。\n\n"
        "如果你在电脑边：双击桌面上的「修复小鲸鱼.bat」，\n"
        "它会重启 NapCat、把二维码下载到桌面并打开。\n\n"
        "—— 小鲸鱼"
    )
    return send_mail(subject, body)


def notify_qrcode(when: str) -> bool:
    """把登录二维码当附件发过去，人在外面也能扫。"""
    subject = "小鲸鱼：登录二维码（2 分钟内有效）"
    body = (
        f"时间：{when}\n\n"
        "掉线了，这是刚生成的新二维码，附件里。\n\n"
        "重要：QQ 不允许用「相册里的图片」扫码（会提示登录失败），\n"
        "必须用摄像头对着屏幕扫。所以你需要另一块屏幕：\n"
        "  1. 在电脑 / 平板 / 另一台手机上打开这封邮件\n"
        "  2. 把附件里的二维码显示出来\n"
        "  3. 用手机 QQ「扫一扫」对着那块屏幕扫\n\n"
        "二维码大约 2 分钟过期，过期就再发一封主题带「扫码」的邮件。\n\n"
        "—— 小鲸鱼"
    )
    return send_mail(subject, body, image=config.QRCODE_PATH)


def notify_online(when: str) -> bool:
    return send_mail("小鲸鱼回来了", f"时间：{when}\n\nQQ 重新上线了，可以继续聊了。\n\n—— 小鲸鱼")


# 主题里出现这些词，就认为是在要二维码
QR_SUBJECT_HINTS = ("扫码", "二维码", "qr", "QR", "要码", "登录")


def _decode_header(raw: str) -> str:
    from email.header import decode_header

    parts = []
    for chunk, charset in decode_header(raw or ""):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", "replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def check_trigger_mail(max_scan: int = 5) -> str | None:
    """看收件箱里有没有"要二维码"的邮件。

    只看未读，命中就标记已读，避免重复触发。
    返回触发邮件的主题（用来回信），没有就返回 None。
    """
    if not config.MAIL_IMAP_ENABLED:
        return None
    if not (config.MAIL_FROM and config.MAIL_AUTH_CODE):
        return None

    import imaplib

    try:
        box = imaplib.IMAP4_SSL(
            config.MAIL_IMAP_HOST, config.MAIL_IMAP_PORT, timeout=25
        )
        box.login(config.MAIL_FROM, config.MAIL_AUTH_CODE)
    except Exception as exc:
        log.warning("连不上邮箱（IMAP）：%s", exc)
        return None

    try:
        box.select("INBOX")
        typ, data = box.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return None

        ids = data[0].split()[-max_scan:]
        for mail_id in reversed(ids):
            typ, fetched = box.fetch(
                mail_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])"
            )
            if typ != "OK" or not fetched:
                continue
            raw = b""
            for chunk in fetched:
                if isinstance(chunk, tuple):
                    raw = chunk[1]
                    break
            text = raw.decode("utf-8", "replace")
            subject = ""
            sender = ""
            for line in text.splitlines():
                low = line.lower()
                if low.startswith("subject:"):
                    subject = _decode_header(line.split(":", 1)[1]).strip()
                elif low.startswith("from:"):
                    sender = line.split(":", 1)[1].strip()

            # 排除机器人自己发的信：我们所有自动邮件的主题都以「小鲸鱼」开头。
            # 不能按发件人排除——主人也是用同一个邮箱发信过来的。
            if subject.startswith("小鲸鱼"):
                box.store(mail_id, "+FLAGS", "\\Seen")
                continue

            if any(hint in subject for hint in QR_SUBJECT_HINTS):
                box.store(mail_id, "+FLAGS", "\\Seen")
                log.info("收到要二维码的邮件：%s（来自 %s）", subject[:40], sender[:40])
                return subject
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return None


def notify_qrcode_requested(when: str, subject: str) -> bool:
    """回应"我要二维码"的邮件。"""
    head = "小鲸鱼：你要的登录二维码"
    body = (
        f"时间：{when}\n"
        f"你发的主题：{subject[:60]}\n\n"
        "二维码在附件里。\n\n"
        "重要：QQ 不允许用相册图片扫码，必须摄像头对着屏幕扫。\n"
        "所以请在另一块屏幕（电脑 / 平板 / 别的手机）上打开这封邮件，\n"
        "把二维码显示出来，再用手机 QQ「扫一扫」扫它。\n\n"
        "二维码约 2 分钟过期，过期了就再发一封带「扫码」的邮件。\n\n"
        "—— 小鲸鱼"
    )
    return send_mail(head, body, image=config.QRCODE_PATH)
