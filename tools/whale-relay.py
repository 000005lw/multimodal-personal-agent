"""小鲸鱼中转 —— 一个程序把 SSH 隧道和家里客户端全拉起来。

双击 whale-relay.bat 即可。窗口开着她就借得到你的网，关了她就借不到。
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# 默认使用当前仓库目录，也允许通过环境变量覆盖
WORKSPACE = os.environ.get("WHALE_WORKSPACE") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, WORKSPACE)
os.chdir(WORKSPACE)

from whale.integrations import home_agent  # noqa: E402  复用里面的抓取逻辑

# home_agent imports config, which loads .env before these values are read.
SERVER = os.environ.get("HOME_SSH_TARGET", "").strip()
SSH_PORT = os.environ.get("HOME_SSH_PORT", "").strip()
LOCAL_PORT = int(os.environ.get("HOME_RELAY_PORT", "8091"))
REMOTE = os.environ.get("HOME_REMOTE", "127.0.0.1:8090").strip()


def banner(text):
    print("=" * 52)
    print("  " + text)
    print("=" * 52)


def start_tunnel():
    """拉起 SSH 隧道。它会一直挂着，直到我们主动结束。"""
    cmd = [
        "ssh",
        "-N",
        # 保活调紧：20 秒太宽松，机房/家庭路由会在空闲时静默切断，
        # 症状就是日志里的 WinError 10054「远程主机强迫关闭了一个现有的连接」
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=10",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "TCPKeepAlive=yes",
        # 有密钥就走密钥，不会卡在密码提示上；没有密钥会立刻失败而不是干等
        "-o", "BatchMode=yes",
        "-L", f"{LOCAL_PORT}:{REMOTE}",
    ]
    if SSH_PORT:
        cmd.extend(["-p", SSH_PORT])
    cmd.append(SERVER)
    print(f"[隧道] 正在连接 {SERVER} …")
    return subprocess.Popen(cmd)


def tunnel_alive(proc) -> bool:
    if proc is None or proc.poll() is not None:
        return False
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{LOCAL_PORT}/health", timeout=3
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def wait_tunnel(proc, timeout=90) -> bool:
    for _ in range(timeout):
        if proc.poll() is not None:
            return False
        if tunnel_alive(proc):
            return True
        time.sleep(1)
    return False


PENDING_DIR = os.path.join(
    os.environ.get("TEMP") or os.environ.get("TMP") or ".", "whale_pending_report"
)


def _save_pending(job_id, result):
    """先把结果落盘。程序崩了/断电了，下次启动还能补交。"""
    try:
        os.makedirs(PENDING_DIR, exist_ok=True)
        path = os.path.join(PENDING_DIR, f"{job_id}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"id": job_id, "result": result}, fh, ensure_ascii=False)
        return path
    except Exception as exc:
        print(f"  [~] 结果没存下来（不影响发送）：{exc}")
        return ""


def _drop_pending(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def deliver(job_id, result, tries=40, delay=3.0):
    """交作业。失败就等隧道重连再交，最多等约两分钟。返回成功与否。"""
    saved = _save_pending(job_id, result)
    for attempt in range(1, tries + 1):
        try:
            home_agent.report(job_id, result)
            _drop_pending(saved)
            if attempt > 1:
                print(f"  [√] 重试 {attempt - 1} 次后交上了")
            return True
        except Exception as exc:
            if attempt == 1 or attempt % 5 == 0:
                print(f"  [!] 交作业失败（{exc}），{delay:.0f} 秒后重试（{attempt}/{tries}）")
            time.sleep(delay)
    print("  [X] 试了很多次都交不上，结果留在本地，下次启动会补交")
    return False


def flush_pending():
    """启动时把上次没交上的结果补交。"""
    if not os.path.isdir(PENDING_DIR):
        return
    files = [f for f in os.listdir(PENDING_DIR) if f.endswith(".json")]
    if not files:
        return
    print(f"[补交] 发现 {len(files)} 份没交上的结果，正在补…")
    for name in files:
        path = os.path.join(PENDING_DIR, name)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            home_agent.report(data.get("id"), data.get("result"))
            _drop_pending(path)
            print(f"  [√] 补交成功：{name}")
        except Exception as exc:
            print(f"  [!] 补交失败（{exc}），下次再试")
            break


def main():
    banner("小鲸鱼中转 已启动")
    print("  这个窗口别关，关了她就借不到你的网了")
    print("  按 Ctrl+C 退出")
    print()

    if not SERVER:
        print("[!] 缺少 HOME_SSH_TARGET：请填写 ubuntu@你的服务器地址")
        return 2

    try:
        home_agent.ensure_config()
    except RuntimeError as exc:
        print(f"[!] {exc}")
        return 2

    flush_pending()

    tunnel = None
    fails = 0
    last_health = 0.0

    try:
        while True:
            # 隧道挂了就重连
            if not tunnel_alive(tunnel):
                if tunnel is not None:
                    print("[隧道] 断了，重连中…")
                    try:
                        tunnel.terminate()
                    except Exception:
                        pass
                tunnel = start_tunnel()
                if not wait_tunnel(tunnel):
                    print("[隧道] 连不上。检查网络，或者服务器是否在跑。")
                    print("       20 秒后重试")
                    time.sleep(20)
                    continue
                print("[隧道] 已连通\n")

            # 每 30 秒体检一次隧道
            now = time.time()
            if now - last_health > 30:
                last_health = now
                if not tunnel_alive(tunnel):
                    continue

            # 去服务器领活
            try:
                data = home_agent.poll()
                fails = 0
            except (urllib.error.URLError, OSError) as exc:
                fails += 1
                if fails in (1, 10, 50):
                    print(f"[!] 领活失败（{exc}），继续重试")
                time.sleep(3)
                continue

            job = data.get("job")
            if not job:
                continue

            print(f"[活] {job.get('kind')} {str(job.get('url'))[:70]}")

            # 干活期间隧道断了（日志里的 "channel 2: open failed: Connection refused"），
            # 处理函数会抛连接类异常。以前直接 result=None 交上去，这一段就白做了。
            # 现在：等隧道恢复后重做，最多 3 次。
            result = None
            for attempt in range(1, 4):
                try:
                    result = home_agent.handle(job)
                    break
                except Exception as exc:
                    conn_like = isinstance(
                        exc, (ConnectionError, TimeoutError, OSError)
                    ) or "refused" in str(exc).lower() or "closed" in str(exc).lower()
                    if attempt < 3 and conn_like:
                        print(f"  [!] 处理时连接断了（{type(exc).__name__}），"
                              f"等隧道恢复后重做（{attempt}/3）")
                        for _ in range(20):
                            if tunnel_alive(tunnel):
                                break
                            time.sleep(3)
                        time.sleep(2)
                        continue
                    print(f"  [!] 处理出错：{type(exc).__name__} {exc}")
                    result = None
                    break

            if isinstance(result, dict):
                if "items" in result:
                    print(f"  -> 热门列表 {len(result.get('items') or [])} 条")
                else:
                    print(
                        f"  -> 文字 {len(str(result.get('text') or ''))} 字，"
                        f"{len(result.get('frames') or [])} 帧，"
                        f"转写 {len(str(result.get('transcript') or ''))} 字"
                    )
            else:
                print("  ->", (str(result)[:60] if result else "没读到"))

            # 交作业要重试：隧道刚断时 POST 会失败，但结果还在这儿，
            # 等隧道重连上再交一次就行。不重试的话这一段白做。
            deliver(job["id"], result)
    except KeyboardInterrupt:
        print("\n收到退出信号，正在收尾…")
    finally:
        if tunnel is not None:
            try:
                tunnel.terminate()
                tunnel.wait(timeout=5)
            except Exception:
                pass
        print("已退出。")


if __name__ == "__main__":
    raise SystemExit(main())
