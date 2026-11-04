"""她能看到的"自己"。

以前的 `[[查:...]]` 只能查歌单、记忆、说说这些业务数据，
看不了"我跑在哪台机器上、还剩多少内存、刚才有没有报错"。

这个模块全是只读操作：读 /proc、df、journalctl、目录大小。
不写文件、不改服务、不执行用户输入的命令。
"""

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import config

log = logging.getLogger("whale.sysinfo")

DATA_DIR = config.DATA_DIR


def _read(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return default


def _run(cmd: list[str], timeout: int = 10) -> str:
    """只跑固定的命令，参数都是本文件写死的，不接受外部输入。"""
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, errors="replace"
        )
        return (done.stdout or "").strip()
    except Exception as exc:
        log.debug("命令失败 %s：%s", cmd[:2], exc)
        return ""


def system() -> str:
    """CPU / 内存 / 磁盘 / 跑了多久。"""
    lines = []

    load = _read("/proc/loadavg").split()
    if load:
        lines.append(f"负载：{load[0]}（1 分钟）")

    meminfo = {}
    for line in _read("/proc/meminfo").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meminfo[k.strip()] = v.strip()

    def mb(key: str) -> int:
        try:
            return int(meminfo.get(key, "0 kB").split()[0]) // 1024
        except Exception:
            return 0

    total, avail = mb("MemTotal"), mb("MemAvailable")
    if total:
        lines.append(
            f"内存：{total - avail} / {total} MB 在用，还剩 {avail} MB"
        )
    swap_total = mb("SwapTotal")
    if swap_total:
        lines.append(f"交换区：{swap_total - mb('SwapFree')} / {swap_total} MB 在用")

    usage = shutil.disk_usage("/")
    lines.append(
        f"磁盘：{usage.used // 2**30} / {usage.total // 2**30} GB 在用，"
        f"剩 {usage.free // 2**30} GB"
    )

    uptime = _read("/proc/uptime").split()
    if uptime:
        secs = int(float(uptime[0]))
        lines.append(f"开机已经 {secs // 86400} 天 {secs % 86400 // 3600} 小时")

    cpus = os.cpu_count() or 0
    if cpus:
        lines.append(f"核心数：{cpus}")
    return "\n".join(lines) or "系统信息读不出来"


def files() -> str:
    """她自己的数据目录：各存了多少、占多大。"""
    if not DATA_DIR.exists():
        return "数据目录还不存在"

    rows = []
    for child in sorted(DATA_DIR.iterdir()):
        if ".bak" in child.name or ".namebak" in child.name or ".phbak" in child.name:
            continue
        try:
            if child.is_dir():
                count = sum(1 for p in child.rglob("*") if p.is_file())
                size = sum(p.stat().st_size for p in child.rglob("*") if p.is_file())
            else:
                count = 1
                size = child.stat().st_size
        except Exception:
            continue
        if count:
            rows.append((child.name, count, size))

    if not rows:
        return "数据目录是空的"

    total_size = sum(s for _, _, s in rows)
    lines = [f"我的数据一共 {total_size / 2**20:.1f} MB："]
    for name, count, size in rows:
        lines.append(f"· {name}：{count} 个文件，{size / 2**20:.1f} MB")
    return "\n".join(lines)


def logs(limit: int = 8) -> str:
    """最近的报错和警告。"""
    out = _run(
        ["journalctl", "--user", "-u", "whale-bot", "-n", "300", "--no-pager"], 15
    )
    if not out:
        return "日志读不出来（可能不是 systemd 起的）"

    bad = [
        line.strip()
        for line in out.splitlines()
        if ("WARNING" in line or "ERROR" in line or "Traceback" in line)
    ]
    if not bad:
        return "最近 300 行日志里没有报错，一切正常"

    tail = bad[-limit:]
    lines = [f"最近有 {len(bad)} 条报错/警告，最后 {len(tail)} 条是："]
    lines.extend("· " + line[-90:] for line in tail)
    return "\n".join(lines)


def service() -> str:
    """自己的服务、SnowLuma、中继，各自活着没。"""
    lines = []

    active = _run(["systemctl", "--user", "is-active", "whale-bot"]) or "未知"
    uptime = _run(["systemctl", "--user", "show", "-p", "ActiveEnterTimestamp", "whale-bot"])
    lines.append(f"我自己：{active}")
    if uptime and "=" in uptime:
        lines.append(f"· 启动于 {uptime.split('=', 1)[1]}")

    # 机器人是普通用户，docker 要 sudo（机器上配了 NOPASSWD）
    docker = _run(["docker", "ps", "--format", "{{.Names}} {{.Status}}"]) or _run(
        ["sudo", "-n", "docker", "ps", "--format", "{{.Names}} {{.Status}}"]
    )
    if docker:
        lines.append(f"SnowLuma：{docker.splitlines()[0]}")
    else:
        lines.append("SnowLuma：没查到容器（可能没权限或没在跑）")

    try:
        req = urllib.request.Request(
            config.ONEBOT_API_BASE + "/get_status",
            headers={"Authorization": f"Bearer {config.ONEBOT_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        online = (data.get("data") or {}).get("online")
        lines.append(f"QQ 连接：{'在线' if online else '不在线'}")
    except Exception as exc:
        lines.append(f"QQ 连接：连不上（{type(exc).__name__}）")

    return "\n".join(lines)


def content() -> str:
    """她记得/会说的东西有多少。"""
    lines = []
    try:
        lines.append(f"曲库：{_songs()} 首")
    except Exception:
        pass
    try:
        from stickers import available

        lines.append(f"表情包：{len(available())} 类")
    except Exception:
        pass
    try:
        import songlib

        segs = 0
        for name in songlib.names(200):
            hit = songlib.find(name)
            segs += len(songlib.segments(hit)) if hit else 0
        lines.append(f"曲库分段：{segs} 段")
    except Exception:
        pass
    try:
        mem = DATA_DIR / "memory.json"
        if mem.exists():
            data = json.loads(mem.read_text(encoding="utf-8"))
            conv = data.get("conversations") or {}
            total = sum(len((v or {}).get("messages") or []) for v in conv.values())
            lines.append(f"记忆：{len(conv)} 个会话，{total} 条消息")
    except Exception:
        pass
    return "\n".join(lines)


def _songs() -> int:
    try:
        import songlib

        return len(songlib.names(500))
    except Exception:
        return 0


def overview() -> str:
    """一句话总览，塞进提示词用。"""
    parts = []
    try:
        usage = shutil.disk_usage("/")
        meminfo = _read("/proc/meminfo")
        avail = 0
        for line in meminfo.splitlines():
            if line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) // 1024
        parts.append(f"磁盘剩 {usage.free // 2**30} GB，内存剩 {avail} MB")
    except Exception:
        pass
    try:
        parts.append(f"曲库 {_songs()} 首")
    except Exception:
        pass
    return "；".join(parts)
