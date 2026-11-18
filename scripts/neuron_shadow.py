"""影子模式：不改 bot.py，只偷偷算"如果用了神经元记忆，会想起什么"，然后打分。

    python neuron_shadow.py                 # 跑一遍，出报告
    python neuron_shadow.py --limit 80      # 每人只用最近 80 条对话做样本

怎么打分（关键，不能只看"看起来像"）：
  拿真实的下一句回复当"标准答案"。
  对比两组：
    A 神经元激活出来的 6 条记忆
    B 随机抽的 6 条记忆（对照）
  分别算它们跟标准答案的语义相关度（取这 6 条里最高的那个），
  看 A 是否明显高于 B。高得越多，说明这套检索真有用。
"""

from __future__ import annotations

import argparse
import json
import random
import time

import numpy as np

from whale import config
from whale.memory import neuron

BASE = config.BASE_DIR
MEM = config.DATA_DIR / "memory.json"
OUT = config.DATA_DIR / "shadow.jsonl"


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if not na or not nb:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def evaluate(samples: int = 24, limit: int = 120, seed: int = 11) -> dict:
    """跑一轮 A/B，返回指标（不打印）。给定时任务用。"""
    conn = neuron.connect()
    rows = conn.execute("SELECT id, text, vec FROM neuron").fetchall()
    if len(rows) < 10:
        return {"ok": False, "why": "记忆太少"}
    all_vecs = [neuron._blob_to_vec(r["vec"]) for r in rows]

    convs = json.loads(MEM.read_text(encoding="utf-8")).get("conversations") or {}
    pool = []
    for cid, conv in convs.items():
        msgs = ((conv or {}).get("messages") or [])[-limit:]
        for i in range(len(msgs) - 1):
            if msgs[i].get("role") == "user" and msgs[i + 1].get("role") == "assistant":
                pool.append((cid, msgs[i]["content"], msgs[i + 1]["content"]))
    random.seed(seed)
    random.shuffle(pool)
    pool = pool[:samples]
    if not pool:
        return {"ok": False, "why": "没有样本"}

    got_n, got_r = [], []
    for cid, query, answer in pool:
        truth = neuron.embed(str(answer)[:200])
        hits = neuron.activate(conn, str(query)[:200], cid=cid, k=6)
        if not hits:
            continue
        best_n = max(cosine(truth, neuron.embed(h["text"])) for h in hits)
        best_r = max(
            cosine(truth, all_vecs[random.randrange(len(all_vecs))]) for _ in hits
        )
        got_n.append(best_n)
        got_r.append(best_r)
    if not got_n:
        return {"ok": False, "why": "全部没激活到东西"}

    mean_n = float(np.mean(got_n))
    mean_r = float(np.mean(got_r))
    win = sum(1 for a, b in zip(got_n, got_r) if a > b)
    return {
        "ok": True,
        "n": len(got_n),
        "neuron": round(mean_n, 4),
        "random": round(mean_r, 4),
        "lift": round((mean_n - mean_r) / max(mean_r, 1e-6) * 100, 1),
        "win": win,
        "win_rate": round(win / len(got_n) * 100, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--samples", type=int, default=40, help="总共取多少个问句")
    args = ap.parse_args()

    conn = neuron.connect()
    rows = conn.execute("SELECT id, text, vec FROM neuron").fetchall()
    if len(rows) < 10:
        print("记忆太少了，先跑 neuron_import.py")
        return
    all_vecs = [neuron._blob_to_vec(r["vec"]) for r in rows]

    convs = json.loads(MEM.read_text(encoding="utf-8")).get("conversations") or {}
    samples = []
    for cid, conv in convs.items():
        msgs = (conv or {}).get("messages") or []
        msgs = msgs[-args.limit :]
        for i in range(len(msgs) - 1):
            if msgs[i].get("role") == "user" and msgs[i + 1].get("role") == "assistant":
                samples.append((cid, msgs[i]["content"], msgs[i + 1]["content"]))
    random.seed(11)
    random.shuffle(samples)
    samples = samples[: args.samples]
    if not samples:
        print("没有样本")
        return

    print(f"样本 {len(samples)} 个，记忆库 {len(rows)} 条\n")
    hits_n, hits_r = [], []
    records = []
    for idx, (cid, query, answer) in enumerate(samples, 1):
        truth = neuron.embed(str(answer)[:200])
        got = neuron.activate(conn, str(query)[:200], cid=cid, k=6)
        if not got:
            continue
        got_scores = []
        for g in got:
            v = neuron.embed(g["text"])
            got_scores.append(cosine(truth, v))
        # 对照：同样数量随机抽
        rand_scores = [
            cosine(truth, all_vecs[random.randrange(len(all_vecs))]) for _ in got
        ]
        best_n, best_r = max(got_scores), max(rand_scores)
        hits_n.append(best_n)
        hits_r.append(best_r)
        records.append(
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "cid": cid,
                "query": str(query)[:80],
                "answer": str(answer)[:100],
                "activated": [g["text"] for g in got],
                "best_neuron": round(best_n, 4),
                "best_random": round(best_r, 4),
            }
        )
        if idx <= 5:
            print(f"[{idx}] 他说：{str(query)[:40]}")
            print(f"    她实际回：{str(answer)[:50]}")
            print(f"    激活出来的：{[g['text'][:22] for g in got[:3]]}")
            print(f"    相关度 神经元 {best_n:.3f} vs 随机 {best_r:.3f}\n")

    if not hits_n:
        print("没算出结果")
        return

    with OUT.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    mn, mr = float(np.mean(hits_n)), float(np.mean(hits_r))
    win = sum(1 for a, b in zip(hits_n, hits_r) if a > b)
    print("=" * 46)
    print(f"神经元激活平均相关度  {mn:.4f}")
    print(f"随机抽的平均相关度    {mr:.4f}")
    print(f"提升                {(mn - mr) / max(mr, 1e-6) * 100:.1f}%")
    print(f"胜率                {win}/{len(hits_n)}（{win/len(hits_n)*100:.0f}%）")
    print(f"明细已写入 {OUT}")


if __name__ == "__main__":
    main()
