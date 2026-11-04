"""把 memory.json 里的旧聊天记录，提炼成神经元记忆节点。

    python neuron_import.py --cid u10000000        # 只导一个人
    python neuron_import.py --all --limit 200       # 全导，每人最多 200 条
    python neuron_import.py --all --dry             # 只看会提出什么，不写库

为什么不能直接把原话塞进去：
    "吃了这个" / "那是土豆不是包子" 这种是对话碎片，不是记忆。
    记忆应该是「爸爸在增肌，会算蛋白质」这种能站得住的事实。

做法：把对话按 12 条一批交给模型，让它抽原子事实 + 归类 + 打情绪值，
同一批抽出来的互相连边（赫布学习）。重复的会自动合并。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

import neuron

BASE = Path(__file__).parent
MEM = BASE / "data" / "memory.json"

PROMPT = """你在帮一个 AI 整理她的长期记忆。

下面是他们的聊天记录。请抽出**长期有用的记忆**。

【称呼必须统一】——这一条最容易写错：
- 她自己，一律写「我」（不要写"AI""米米""她"）
- 跟她说话的这个人是「{who}」（不要写"他""用户"）
- 其他朋友，用聊天里出现的名字；没名字就写「一个朋友」
- 绝不能用「他」来指这个人

【只记这种东西】
- 稳定的事实：住哪、做什么工作、在准备什么、有什么习惯
- 偏好：喜欢吃什么、爱看什么、讨厌什么
- 重要事件：面试、搬走、过生日、吵架、答应过的事
- 情绪基调：最近很累、最近很开心（要写明是因为什么）

【绝对不要记】
- 临时的系统状态：「报错」「卡顿」「日志里有几条警告」这类，过一天就没意义
- 关于"我是 AI/机器人/怎么称呼自己"的元信息
- 寒暄、语气词、单句碎片
- 她自己瞎猜的、聊天里没依据的东西
- 编造。聊天里没有的，一个字都不要写

每条一句话，20 字以内，必须能独立看懂。
kind 只能是：fact / prefer / event / mood / person
valence 是情绪色彩，-1 到 1，中性写 0。

输出**纯 JSON 数组**，不要解释。格式：
[{{"text": "爸爸在准备秋招", "kind": "fact", "valence": 0.0}}]

聊天记录：
{chat}
"""


def _key() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def _extract(chat: str, who: str = "对方") -> list[dict]:
    key = _key()
    if not key:
        return []
    try:
        resp = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": PROMPT.format(chat=chat, who=who)}],
                "temperature": 0.3,
                "max_tokens": 1200,
            },
            timeout=90,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        print(f"    ! 抽取出错：{type(exc).__name__}")
        return []

    content = content.strip()
    content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
    start, end = content.find("["), content.rfind("]")
    if start < 0 or end < 0:
        return []
    try:
        items = json.loads(content[start : end + 1])
    except Exception:
        return []

    out = []
    for it in items if isinstance(items, list) else []:
        text = str(it.get("text", "")).strip()
        kind = str(it.get("kind", "fact")).strip()
        if not text or kind not in neuron.TAU:
            continue
        # 临时系统状态不是记忆（"日志有 3 条警告""刚才卡了一下"）
        if re.search(r"报错|警告|日志|卡顿|崩溃|异常|接口|超时", text):
            continue
        # 关于"我是 AI/机器人"的元信息也不是记忆
        if re.search(r"\bAI\b|人工智能|机器人|自称|扮演", text):
            continue
        if len(text) > 40:
            continue
        try:
            valence = max(-1.0, min(1.0, float(it.get("valence", 0))))
        except Exception:
            valence = 0.0
        out.append({"text": text, "kind": kind, "valence": valence})
    return out


def _owner_cids() -> set[str]:
    """从 .env 里认出谁是主人，只有主人才叫「爸爸」。"""
    out = set()
    env = BASE / ".env"
    if not env.exists():
        return out
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("OWNER_QQ=") or line.strip().startswith("OWNER_QQS="):
            val = line.split("=", 1)[1].strip()
            for part in re.split(r"[,\s]+", val):
                if part.isdigit():
                    out.add("u" + part)
                    out.add(part)
    return out


def _who(cid: str) -> str:
    if cid in _owner_cids():
        return "爸爸"
    # 朋友：看看通讯录里有没有记下名字
    try:
        import json as _json
        from pathlib import Path as _P

        contacts = _json.loads((BASE / "data" / "contacts.json").read_text(encoding="utf-8"))
        qq = cid[1:] if cid.startswith("u") else cid
        name = (contacts.get(qq) or {}).get("name")
        if name:
            return name
    except Exception:
        pass
    return "对方"


def load_conversations() -> dict:
    data = json.loads(MEM.read_text(encoding="utf-8"))
    return data.get("conversations") or {}


def import_conversation(conn, cid: str, conv: dict, limit: int, dry: bool, batch: int = 12) -> int:
    msgs = (conv.get("messages") or [])[-limit:]
    notes = conv.get("notes") or []
    if not msgs and not notes:
        return 0

    added = 0

    # 长期备注是最可靠的事实，直接进，强度拉满
    for note in notes:
        text = str(note).strip()
        if not text:
            continue
        if dry:
            print(f"    [备注] {text[:40]}")
            continue
        neuron.add_node(conn, text, kind="fact", valence=0.2, cid=cid)
        added += 1

    for i in range(0, len(msgs), batch):
        chunk = msgs[i : i + batch]
        lines = []
        for m in chunk:
            who = "他" if m.get("role") == "user" else "我"
            content = str(m.get("content") or "").strip().replace("\n", " ")
            if content:
                lines.append(f"{who}：{content[:200]}")
        if not lines:
            continue
        facts = _extract("\n".join(lines), who=_who(cid))
        print(f"    第 {i//batch+1} 批（{len(chunk)} 条）→ 抽出 {len(facts)} 条记忆")
        if dry:
            for f in facts:
                print(f"        [{f['kind']}] {f['text']}")
            continue
        ids = []
        for f in facts:
            ids.append(neuron.add_node(conn, f["text"], kind=f["kind"], valence=f["valence"], cid=cid))
        if len(ids) > 1:
            neuron.coactivate(conn, ids)
        added += len(ids)
    conn.commit()
    return added


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cid", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    convs = load_conversations()
    if args.cid:
        convs = {args.cid: convs.get(args.cid)} if args.cid in convs else {}
    elif not args.all:
        print("要么 --cid <会话>，要么 --all。现有会话：")
        for cid, c in convs.items():
            print(f"  {cid}  {len((c or {}).get('messages') or [])} 条")
        return

    conn = neuron.connect()
    total = 0
    for cid, conv in convs.items():
        if not conv:
            continue
        print(f"\n=== {cid} ===")
        n = import_conversation(conn, cid, conv, args.limit, args.dry)
        total += n
        print(f"  入库 {n} 条")

    if not args.dry:
        neuron.decay(conn)
        st = neuron.stats(conn)
        print(f"\n合计 {total} 条。库里现在：节点 {st['nodes']} · 突触 {st['synapses']}")
        for kind, info in sorted(st["by_kind"].items()):
            print(f"  {kind:8s} {info['count']:4d} 条")


if __name__ == "__main__":
    main()
