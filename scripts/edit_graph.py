"""编辑记忆云：拉库 → 开编辑页 → 关掉后把改动传回服务器并应用。

桌面「编辑记忆云.bat」调这个。
流程要点：
1. 先拉最新库（编辑基于最新状态）
2. 起本地编辑服务（neuron_edit.py），浏览器打开
3. 用户按 Ctrl+C 关掉服务后：传补丁 → 服务器应用 → 重启机器人
4. 补丁传不上去就留在本地，下次再跑会补

    python edit_graph.py
    python edit_graph.py --cid u1000000011
    python edit_graph.py --server-only   # 只把本地补丁传上去应用
"""

from __future__ import annotations

import subprocess
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "data"
PATCH = DATA / "neuron_patch.jsonl"
SERVER = os.environ.get("WHALE_SERVER", "ubuntu@YOUR_SERVER_IP")
REMOTE_DIR = "/home/ubuntu/whale-bot"


def _sh(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def pull_db() -> None:
    tmp = DATA / "neuron.db.new"
    DATA.mkdir(parents=True, exist_ok=True)
    r = _sh(["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
             f"{SERVER}:{REMOTE_DIR}/data/neuron.db", str(tmp)])
    if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1024:
        target = DATA / "neuron.db"
        try:
            if target.exists():
                target.unlink()
        except Exception:
            pass
        tmp.replace(target)
        print(f"  已拉到最新记忆库（{target.stat().st_size / 1024:.0f} KB）")
    else:
        tmp.unlink(missing_ok=True)
        print("  拉不到服务器，用本地库继续（可能旧一点）")


def upload_patch() -> bool:
    if not PATCH.exists() or PATCH.stat().st_size == 0:
        print("  没有改动，不用上传")
        return True

    print("  上传补丁…")
    r = _sh(["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
             str(PATCH), f"{SERVER}:{REMOTE_DIR}/data/neuron_patch.jsonl"])
    if r.returncode != 0:
        print("  上传失败，改动留在本地：", PATCH)
        return False

    print("  在服务器上应用…")
    cmd = (
        "cd /home/ubuntu/whale-bot && "
        ".venv/bin/python scripts/neuron_apply_patch.py && "
        "systemctl --user restart whale-bot && "
        "systemctl --user is-active whale-bot"
    )
    r = _sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", SERVER, cmd], timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    print("  " + out.strip().replace("\n", "\n  "))
    if r.returncode != 0:
        print("  应用失败，补丁还在你电脑上，下次重跑会再传一次")
        return False

    PATCH.unlink(missing_ok=True)
    print("  好了，她已经在用改过的记忆")
    return True


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if "--server-only" in sys.argv:
        return 0 if upload_patch() else 1

    print("第一步：拉最新记忆库")
    pull_db()

    print()
    print("第二步：打开编辑页（改完按 Ctrl+C 回到这里）")
    print()
    sys.path.insert(0, str(PROJECT_ROOT))
    from scripts import neuron_edit

    argv_backup = sys.argv[:]
    sys.argv = [sys.argv[0]] + [a for a in argv_backup[1:] if a not in ("--server-only",)]
    try:
        neuron_edit.main()          # 会一直跑到 Ctrl+C
    except KeyboardInterrupt:
        print()
        print("编辑服务已关闭")
    finally:
        sys.argv = argv_backup

    print()
    print("第三步：把改动传回服务器")
    ok = upload_patch()
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
