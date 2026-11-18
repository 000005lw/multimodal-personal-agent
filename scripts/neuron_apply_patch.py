"""把编辑页产生的补丁应用到记忆库。

在服务器上跑（改完点保存之后）：

    python3 neuron_apply_patch.py

做的事：读 data/neuron_patch.jsonl，逐条改 neuron.db，然后把日志改名成
`.applied-<时间>`，不会重复应用。

为什么不是直接传整个 neuron.db：她在你编辑的那几分钟里可能又学了新记忆，
整库覆盖会把那些新记忆抹掉。只传改动就不会。
"""

from __future__ import annotations

import json
import sys
import time

from whale.memory import neuron

LOG = neuron.DATA / "neuron_patch.jsonl"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not LOG.exists():
        print("没有待应用的改动（neuron_patch.jsonl 不存在）")
        return 0

    lines = [ln for ln in LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        print("补丁文件是空的")
        return 0

    conn = neuron.connect()
    done = {"update": 0, "delete": 0, "add": 0}
    failed = 0

    for raw in lines:
        try:
            item = json.loads(raw)
        except Exception:
            failed += 1
            continue
        op = item.get("op")
        try:
            if op == "update":
                ok = neuron.update_node(
                    conn,
                    int(item.get("id") or 0),
                    text=item.get("text"),
                    kind=item.get("kind"),
                    stability=item.get("stability"),
                )
                done["update"] += 1 if ok else 0
                failed += 0 if ok else 1
                if ok:
                    print(f"  改了 #{item['id']}：{str(item.get('text'))[:26]}")
            elif op == "delete":
                ok = neuron.delete_node(conn, int(item.get("id") or 0))
                done["delete"] += 1 if ok else 0
                failed += 0 if ok else 1
                if ok:
                    print(f"  删了 #{item['id']}")
            elif op == "add":
                text = str(item.get("text") or "").strip()
                if text:
                    neuron.add_manual(conn, text, kind=str(item.get("kind") or "fact"),
                                      cid=str(item.get("cid") or ""))
                    done["add"] += 1
                    print(f"  加了：{text[:26]}")
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            print(f"  [!] 一条没成功：{exc}")

    applied = LOG.with_name(f"neuron_patch.applied-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    try:
        LOG.rename(applied)
    except Exception:
        pass

    print()
    print(f"完成：改 {done['update']} 条、删 {done['delete']} 条、加 {done['add']} 条，失败 {failed} 条")
    print(f"日志已归档：{applied.name}")
    if done["update"] or done["delete"] or done["add"]:
        print("记得重启服务，让她用上新记忆：systemctl --user restart whale-bot")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
