"""拉最新记忆库 → 画图 → 打开浏览器。

桌面的「看记忆云.bat」就是调这个。之所以不写在 .bat 里：
cmd 按 GBK 解析 .bat，文件内容里有中文会变成乱码路径（踩过）。
这里全部用 Python，编码自己控制。

    python view_graph.py            # 拉库、画图、打开
    python view_graph.py --no-open  # 只生成不打开
    python view_graph.py --no-pull  # 不拉服务器，直接画本地库
    python view_graph.py --cid u1000000011
"""

from __future__ import annotations

import subprocess
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = os.environ.get("WHALE_SERVER", "ubuntu@YOUR_SERVER_IP")
REMOTE_DB = "/home/ubuntu/whale-bot/data/neuron.db"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = sys.argv[1:]
    cid = ""
    for i, a in enumerate(args):
        if a == "--cid" and i + 1 < len(args):
            cid = args[i + 1]

    if "--no-pull" not in args:
        print("从服务器拉最新的记忆库…")
        tmp = HERE / "data" / "neuron.db.new"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(
            ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
             f"{SERVER}:{REMOTE_DB}", str(tmp)],
            capture_output=True, text=True,
        )
        if done.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1024:
            target = HERE / "data" / "neuron.db"
            try:
                if target.exists():
                    target.unlink()
            except Exception:
                pass
            tmp.replace(target)
            print(f"  拿到了（{target.stat().st_size / 1024:.0f} KB）")
        else:
            tmp.unlink(missing_ok=True)
            print("  拉不到服务器，先用本地那份（信息可能旧一点）")

    sys.path.insert(0, str(HERE))
    import neuron_view

    neuron_view.build(open_browser="--no-open" not in args, cid=cid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
