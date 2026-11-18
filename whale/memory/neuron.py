"""小鲸鱼的神经元记忆。

记忆不是一堆聊天记录，是一张会自己长的网：
  节点 = 一件事（事实 / 偏好 / 事件 / 情绪 / 人物）
  突触 = 两件事之间的关系，带权重

四个机制（对应 docs/memory-neuron-design.md）：
  1. 扩散激活 activate()  —— 顺着突触想起相关的事
  2. 赫布学习 coactivate() —— 一起出现的连得更紧
  3. 分级遗忘 decay()      —— 按类型衰减，被想起就回升
  4. 巩固     consolidate() —— 反复被激活才升为长期

存储：sqlite3 + numpy，不依赖任何外部服务。
向量用字符 n-gram 哈希（离线、确定、够用），
以后想换真 embedding，只改 embed() 一个函数。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import numpy as np

from whale import config

DIM = 1024          # 百炼 text-embedding-v3 的输出维度（可改 512 省空间）
HASH_DIM = 256      # 网络不通时的离线兜底维度
DATA = config.DATA_DIR
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "neuron.db"

# ---- 遗忘曲线参数（FSRS / SuperMemo 那一套，源流是艾宾浩斯）----
# 可提取性：R(t) = (1 + F * t / S) ^ (-DECAY)
#   S = 稳定性（天），定义为 R(S) ≈ 0.9 的那个时间点
#   不是纯指数：幂律才有"陡降 + 长尾"的形状，这也是艾宾浩斯曲线的特征
FACTOR = 19.0 / 81.0
DECAY = 0.5

# 各类记忆的初始稳定性（天）：R=0.9 能撑多久
STABILITY = {
    "prefer": 5.0,
    "fact": 3.0,
    "event": 1.2,
    "mood": 0.4,
    "person": 6.0,
}
TAU = STABILITY          # 旧名字保留，别处还在引用
DEFAULT_KIND = "fact"
MAX_STABILITY = 365.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS neuron (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  cid       TEXT    NOT NULL DEFAULT 'u0',
  kind      TEXT    NOT NULL DEFAULT 'fact',
  text      TEXT    NOT NULL,
  vec       BLOB,
  strength  REAL    NOT NULL DEFAULT 1.0,   -- 缓存的可提取性 R（0~1）
  stability REAL    NOT NULL DEFAULT 3.0,   -- 稳定性 S（天）
  reps      INTEGER NOT NULL DEFAULT 0,     -- 成功回忆次数
  lapses    INTEGER NOT NULL DEFAULT 0,     -- 想不起来的次数
  valence   REAL    NOT NULL DEFAULT 0.0,
  created   REAL    NOT NULL,
  last_hit  REAL    NOT NULL,               -- 上次"被想起"的时间
  hits      INTEGER NOT NULL DEFAULT 0,
  UNIQUE(cid, text)
);
CREATE TABLE IF NOT EXISTS synapse (
  a    INTEGER NOT NULL,
  b    INTEGER NOT NULL,
  w    REAL    NOT NULL DEFAULT 0.05,
  kind TEXT    NOT NULL DEFAULT 'cooc',
  PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_neuron_cid ON neuron(cid);
CREATE INDEX IF NOT EXISTS idx_syn_a ON synapse(a);
CREATE INDEX IF NOT EXISTS idx_syn_b ON synapse(b);
"""


def _unique_columns(conn: sqlite3.Connection) -> list[tuple[str, ...]]:
    """Return the column sets protected by UNIQUE indexes."""
    result: list[tuple[str, ...]] = []
    for row in conn.execute("PRAGMA index_list(neuron)").fetchall():
        if not row["unique"]:
            continue
        cols = tuple(
            item["name"]
            for item in conn.execute(
                f"PRAGMA index_info({row['name']!r})"
            ).fetchall()
            if item["name"]
        )
        if cols:
            result.append(cols)
    return result


def _migrate_neuron_unique_scope(conn: sqlite3.Connection) -> bool:
    """Migrate the old global `text UNIQUE` constraint to `UNIQUE(cid, text)`.

    SQLite cannot alter a table-level UNIQUE constraint in place. The table is
    rebuilt transactionally while preserving ids, because synapse.a/b refer to
    neuron.id. A failure rolls the transaction back and leaves the old table
    usable.
    """
    constraints = _unique_columns(conn)
    if ("cid", "text") in constraints or ("text",) not in constraints:
        return False

    columns = [
        row["name"]
        for row in conn.execute("PRAGMA table_info(neuron)").fetchall()
    ]
    if not columns:
        return False

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        old_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM neuron").fetchone()["c"]
        )
        conn.execute("DROP TABLE IF EXISTS neuron_migrated")
        conn.execute(
            """
            CREATE TABLE neuron_migrated (
              id        INTEGER PRIMARY KEY AUTOINCREMENT,
              cid       TEXT    NOT NULL DEFAULT 'u0',
              kind      TEXT    NOT NULL DEFAULT 'fact',
              text      TEXT    NOT NULL,
              vec       BLOB,
              strength  REAL    NOT NULL DEFAULT 1.0,
              stability REAL    NOT NULL DEFAULT 3.0,
              reps      INTEGER NOT NULL DEFAULT 0,
              lapses    INTEGER NOT NULL DEFAULT 0,
              valence   REAL    NOT NULL DEFAULT 0.0,
              created   REAL    NOT NULL,
              last_hit  REAL    NOT NULL,
              hits      INTEGER NOT NULL DEFAULT 0,
              UNIQUE(cid, text)
            )
            """
        )
        quoted = ", ".join(f'"{name}"' for name in columns)
        conn.execute(
            f"INSERT INTO neuron_migrated ({quoted}) "
            f"SELECT {quoted} FROM neuron"
        )
        new_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM neuron_migrated").fetchone()["c"]
        )
        if new_count != old_count:
            raise RuntimeError(
                f"neuron 迁移节点数不一致：旧={old_count}，新={new_count}"
            )
        conn.execute("DROP TABLE neuron")
        conn.execute("ALTER TABLE neuron_migrated RENAME TO neuron")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_neuron_cid ON neuron(cid)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_syn_a ON synapse(a)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_syn_b ON synapse(b)"
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys=ON")
        raise
    conn.execute("PRAGMA foreign_keys=ON")
    return True


# ---------- 基础 ----------

def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # 老库没有 stability/reps/lapses → 补上，并按旧 strength 估一个初始稳定性
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(neuron)")}
    if "stability" not in cols:
        conn.execute("ALTER TABLE neuron ADD COLUMN stability REAL NOT NULL DEFAULT 3.0")
        for r in conn.execute("SELECT id, kind, strength FROM neuron").fetchall():
            base = STABILITY.get(r["kind"], STABILITY[DEFAULT_KIND])
            conn.execute(
                "UPDATE neuron SET stability = ? WHERE id = ?",
                (base * (0.4 + 0.9 * float(r["strength"])), r["id"]),
            )
        conn.commit()
    if "reps" not in cols:
        conn.execute("ALTER TABLE neuron ADD COLUMN reps INTEGER NOT NULL DEFAULT 0")
    if "lapses" not in cols:
        conn.execute("ALTER TABLE neuron ADD COLUMN lapses INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    _migrate_neuron_unique_scope(conn)
    return conn


_WORD = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z]+|\d+")


_KEY = None
_EMB_CACHE_PATH = DATA / "emb_cache.json"
_EMB_CACHE: dict[str, list[float]] = {}


def _api_key() -> str:
    global _KEY
    if _KEY is not None:
        return _KEY
    _KEY = ""
    env = config.BASE_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DASHSCOPE_API_KEY="):
                _KEY = line.split("=", 1)[1].strip()
    return _KEY


def _load_cache() -> dict:
    global _EMB_CACHE, DIM
    if _EMB_CACHE:
        return _EMB_CACHE
    if _EMB_CACHE_PATH.exists():
        try:
            raw = json.loads(_EMB_CACHE_PATH.read_text(encoding="utf-8"))
            _EMB_CACHE = raw.get("vectors", {})
            DIM = int(raw.get("dim", DIM))
        except Exception:
            _EMB_CACHE = {}
    return _EMB_CACHE


def _save_cache() -> None:
    # 只留最近 2000 条，别让文件无限长大
    items = list(_EMB_CACHE.items())[-2000:]
    _EMB_CACHE_PATH.write_text(
        json.dumps({"dim": DIM, "vectors": dict(items)}, ensure_ascii=False),
        encoding="utf-8",
    )


def hash_embed(text: str) -> np.ndarray:
    """离线兜底：字符 n-gram 哈希。注意用 crc32，不能用内置 hash()——
    后者每个进程随机加盐，存进库的向量换个进程就对不上了（踩过）。"""
    import zlib

    units = _WORD.findall(text.lower())
    vec = np.zeros(HASH_DIM, dtype=np.float32)
    if not units:
        return vec
    grams = list(units) + [units[i] + units[i + 1] for i in range(len(units) - 1)]
    for g in grams:
        h = zlib.crc32(g.encode("utf-8"))
        vec[h % HASH_DIM] += 1.0
        vec[(h // HASH_DIM) % HASH_DIM] += 0.5
    vec += 1e-6
    return vec


def embed_many(texts: list[str]) -> list[np.ndarray]:
    """批量取向量：先查缓存，缺的一次请求补齐，失败退回哈希。"""
    cache = _load_cache()
    need = [t for t in dict.fromkeys(texts) if t not in cache]
    key = _api_key()
    if need and key:
        try:
            import httpx

            for i in range(0, len(need), 10):
                batch = need[i : i + 10]
                resp = httpx.post(
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "text-embedding-v3",
                        "input": batch,
                        "dimensions": DIM,
                        "encoding_format": "float",
                    },
                    timeout=40,
                )
                resp.raise_for_status()
                for item, text in zip(resp.json()["data"], batch):
                    cache[text] = item["embedding"]
            _save_cache()
        except Exception:
            pass        # 网络不通就往下走，用哈希兜底

    out = []
    for t in texts:
        if t in cache:
            out.append(np.asarray(cache[t], dtype=np.float32))
        else:
            out.append(hash_embed(t))
    return out


def embed(text: str) -> np.ndarray:
    return embed_many([text])[0]


async def _request_embeddings_async(
    batch: list[str], key: str, dim: int
) -> list[list[float]]:
    """Request one embedding batch without blocking the aiohttp event loop."""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "text-embedding-v3",
                "input": batch,
                "dimensions": dim,
                "encoding_format": "float",
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    return [item["embedding"] for item in data["data"]]


async def embed_many_async(texts: list[str]) -> list[np.ndarray]:
    """Async version used by the bot hot path; hash fallback is unchanged."""
    cache = _load_cache()
    need = [t for t in dict.fromkeys(texts) if t not in cache]
    key = _api_key()
    if need and key:
        try:
            for i in range(0, len(need), 10):
                batch = need[i:i + 10]
                vectors = await _request_embeddings_async(batch, key, DIM)
                for text, vector in zip(batch, vectors):
                    cache[text] = vector
            _save_cache()
        except Exception:
            pass

    out = []
    for text in texts:
        if text in cache:
            out.append(np.asarray(cache[text], dtype=np.float32))
        else:
            out.append(hash_embed(text))
    return out


async def embed_async(text: str) -> np.ndarray:
    return (await embed_many_async([text]))[0]


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n else v


def _vec_to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def _blob_to_vec(blob: bytes | None) -> np.ndarray:
    if not blob:
        return np.zeros(DIM, dtype=np.float32)
    return np.frombuffer(blob, dtype=np.float32)


def _tau(kind: str, valence: float) -> float:
    base = TAU.get(kind, TAU[DEFAULT_KIND])
    # 情绪越强，忘得越慢（杏仁核效应）
    return base * (1.0 + 1.5 * abs(valence))


# ---------- 增删改 ----------

def add_node(
    conn: sqlite3.Connection,
    text: str,
    kind: str = DEFAULT_KIND,
    valence: float = 0.0,
    cid: str = "u0",
    vec: np.ndarray | None = None,
) -> int:
    """加一件事。同样的话只存一条，但会刷新强度（等于又想起一次）。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("空记忆")
    now = time.time()
    row = conn.execute(
        "SELECT id FROM neuron WHERE cid = ? AND text = ?", (cid, text)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE neuron SET strength = MIN(1.0, strength + 0.15), last_hit = ?, "
            "hits = hits + 1 WHERE id = ? AND cid = ?",
            (now, row["id"], cid),
        )
        conn.commit()
        return int(row["id"])

    cur = conn.execute(
        "INSERT INTO neuron (cid, kind, text, vec, strength, stability, valence, "
        "created, last_hit, hits) VALUES (?, ?, ?, ?, 1.0, ?, ?, ?, ?, 1)",
        (
            cid,
            kind,
            text,
            _vec_to_blob(vec if vec is not None else embed(text)),
            STABILITY.get(kind, STABILITY[DEFAULT_KIND]),
            valence,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def link(conn: sqlite3.Connection, a: int, b: int, eta: float = 0.08, kind: str = "cooc") -> None:
    """赫布学习：连过的越来越紧，w 逼近 1 但不会超。"""
    if a == b:
        return
    lo, hi = (a, b) if a < b else (b, a)
    row = conn.execute("SELECT w FROM synapse WHERE a = ? AND b = ?", (lo, hi)).fetchone()
    if row:
        w = min(0.99, float(row["w"]) + eta * (1.0 - float(row["w"])))
        conn.execute("UPDATE synapse SET w = ? WHERE a = ? AND b = ?", (w, lo, hi))
    else:
        conn.execute(
            "INSERT INTO synapse (a, b, w, kind) VALUES (?, ?, ?, ?)",
            (lo, hi, 0.05 + eta, kind),
        )


def coactivate(conn: sqlite3.Connection, ids: list[int], eta: float = 0.08) -> int:
    """同一次里被一起想起的，两两连边。返回新建/加强的边数。"""
    n = 0
    ids = sorted(set(ids))
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            link(conn, ids[i], ids[j], eta=eta)
            n += 1
    conn.commit()
    return n


def decay(conn: sqlite3.Connection, now: float | None = None) -> int:
    """按时间衰减，返回受影响的节点数。幂等：只按上次衰减到现在的时长算。"""
    now = now or time.time()
    row = conn.execute("SELECT v FROM meta WHERE k = 'last_decay'").fetchone()
    last = float(row["v"]) if row else now
    dt_days = max(0.0, (now - last) / 86400.0)
    if dt_days < 0.01:          # 不到一刻钟不动，省得反复算
        return 0

    rows = conn.execute(
        "SELECT id, kind, valence, stability, last_hit FROM neuron"
    ).fetchall()
    now2 = now
    for r in rows:
        # 幂律曲线：R 直接由 S 和距今时长算出来，情绪强的额外加成稳定性
        s = float(r["stability"]) * (1.0 + 0.6 * abs(float(r["valence"])))
        rr = retrievability(s, _elapsed_days(r["last_hit"], now2))
        conn.execute("UPDATE neuron SET strength = ? WHERE id = ?", (rr, r["id"]))
    conn.execute(
        "INSERT INTO meta (k, v) VALUES ('last_decay', ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (str(now),),
    )
    conn.commit()
    return len(rows)


def retrievability(stability: float, elapsed_days: float) -> float:
    """R(t)：隔了这么久，现在还能想起它的概率。

    R = (1 + F·t/S)^-0.5 —— 幂律遗忘曲线。
    t = 0 → 1.0；t = S → 0.9；t = 12.8S → 0.5（半衰点）。
    """
    s = max(0.05, float(stability))
    t = max(0.0, float(elapsed_days))
    return float((1.0 + FACTOR * t / s) ** (-DECAY))


def _elapsed_days(last_hit: float, now: float) -> float:
    return max(0.0, (now - float(last_hit)) / 86400.0)


def hit(conn: sqlite3.Connection, node_id: int, boost: float = 0.0) -> None:
    """被想起 = 成功复习一次。稳定性按"间隔效应"变长：越久没想起来还能想起，涨得越多。

    S' = S × (1 + 0.9 × (1 - R)) × 1.15
    参数 boost 保留兼容（旧代码会传 0.04，这里不用）。
    """
    now = time.time()
    row = conn.execute(
        "SELECT stability, last_hit, reps FROM neuron WHERE id = ?", (node_id,)
    ).fetchone()
    if not row:
        return
    r = retrievability(row["stability"], _elapsed_days(row["last_hit"], now))
    new_s = min(
        MAX_STABILITY,
        max(0.2, float(row["stability"]) * (1.0 + 0.9 * (1.0 - r)) * 1.15),
    )
    conn.execute(
        "UPDATE neuron SET stability = ?, strength = ?, last_hit = ?, "
        "hits = hits + 1, reps = reps + 1 WHERE id = ?",
        (new_s, min(1.0, r + (1.0 - r) * 0.8), now, node_id),
    )


def lapse(conn: sqlite3.Connection, node_id: int) -> None:
    """想不起来：稳定性掉下来（但不清零，和真脑子一样会残留）。"""
    now = time.time()
    conn.execute(
        "UPDATE neuron SET stability = MAX(0.2, stability * 0.3), strength = 0.25, "
        "last_hit = ?, lapses = lapses + 1 WHERE id = ?",
        (now, node_id),
    )


def due_reviews(conn: sqlite3.Connection, cid: str = "", limit: int = 8) -> list[dict]:
    """该复习的那些：R 已经掉到 0.9 以下、但还没忘干净的（甜点区）。

    这就是"间隔重复"的调度依据——不是随机挑，是挑快要忘的。
    """
    now = time.time()
    where = "WHERE cid = ?" if cid else ""
    args = (cid,) if cid else ()
    rows = conn.execute(
        f"SELECT id, cid, text, kind, stability, last_hit, strength FROM neuron {where}",
        args,
    ).fetchall()
    out = []
    for r in rows:
        age = _elapsed_days(r["last_hit"], now)
        if age < 0.02:           # 两小时内刚复习过，跳过
            continue
        rr = retrievability(r["stability"], age)
        if 0.15 <= rr <= 0.80:   # 快忘了但还有救，正是该复习的点
            out.append(
                {
                    "id": int(r["id"]),
                    "text": r["text"],
                    "kind": r["kind"],
                    "r": round(rr, 3),
                    "age_days": round(age, 2),
                    "stability": round(float(r["stability"]), 2),
                }
            )
    out.sort(key=lambda x: x["r"])
    return out[:limit]


# ---------- 检索：扩散激活 ----------

def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    if not a.any() or not b.any() or a.shape != b.shape:
        return 0.0
    return float(np.dot(_unit(a), _unit(b)))


def activate(
    conn: sqlite3.Connection,
    text: str,
    cid: str = "",
    k: int = 6,
    hops: int = 2,
    spread: float = 0.5,
    min_strength: float = 0.15,
    *,
    query_vec: np.ndarray | None = None,
    replacement_vectors: dict[int, np.ndarray] | None = None,
) -> list[dict]:
    """从这句话出发，顺着突触想起一堆相关的事。

    返回按激活值排序的 [{id, text, kind, score, strength}]。
    """
    if cid:
        rows = conn.execute(
            "SELECT id, kind, text, vec, strength, valence FROM neuron WHERE cid = ?",
            (cid,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, kind, text, vec, strength, valence FROM neuron"
        ).fetchall()
    if not rows:
        return []

    query = query_vec if query_vec is not None else embed(text)
    target_dim = int(query.size)

    act: dict[int, float] = {}
    for r in rows:
        vec = _blob_to_vec(r["vec"])
        # 维度不一致 = 当初是哈希兜底存的，重新算一次（换模型后自动修复）
        if vec.size != target_dim:
            replacement = (replacement_vectors or {}).get(int(r["id"]))
            vec = replacement if replacement is not None else embed(r["text"])
            conn.execute("UPDATE neuron SET vec = ? WHERE id = ?",
                         (_vec_to_blob(vec), r["id"]))
        score = _cosine(query, vec)
        if score > 0.30:      # 真向量的基线普遍偏高，门槛要跟着抬
            # 长期强度参与初始激活：快忘光的即使像也不会跳出来
            act[int(r["id"])] = score * (0.30 + 0.70 * float(r["strength"]))
    conn.commit()

    # 只从最像的几条开始扩散，避免从噪音一路传出去
    if act:
        top_score = max(act.values())
        seeds = {k: v for k, v in act.items() if v >= top_score * 0.85}
        act = dict(sorted(seeds.items(), key=lambda kv: -kv[1])[:4])

    for _ in range(max(0, hops)):
        added: dict[int, float] = {}
        for nid, base in list(act.items()):
            for s in conn.execute(
                "SELECT a, b, w FROM synapse WHERE a = ? OR b = ?", (nid, nid)
            ):
                other = s["b"] if s["a"] == nid else s["a"]
                gain = base * float(s["w"]) * spread
                if gain > added.get(other, 0.0):
                    added[other] = gain
        for nid, gain in added.items():
            act[nid] = act.get(nid, 0.0) + gain

    out = []
    by_id = {int(r["id"]): r for r in rows}
    if not act:
        return []
    ceiling = max(act.values())
    for nid, score in sorted(act.items(), key=lambda kv: -kv[1]):
        r = by_id.get(nid)
        if r is None or float(r["strength"]) < min_strength:
            continue
        # 双门槛：相对（不如最相关的那条太多就停）+ 绝对（本身就不够像的不要）
        if score < ceiling * 0.78 or score < 0.36:
            break
        out.append(
            {
                "id": nid,
                "text": r["text"],
                "kind": r["kind"],
                "score": round(score, 4),
                "strength": round(float(r["strength"]), 3),
            }
        )
        if len(out) >= k:
            break
    return out


async def activate_async(
    conn: sqlite3.Connection,
    text: str,
    cid: str = "",
    k: int = 6,
    hops: int = 2,
    spread: float = 0.5,
    min_strength: float = 0.15,
) -> list[dict]:
    """Async activation that requests all missing embeddings off-loop."""
    if cid:
        rows = conn.execute(
            "SELECT id, text, vec FROM neuron WHERE cid = ?", (cid,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id, text, vec FROM neuron").fetchall()
    if not rows:
        return []

    stale = [
        (int(row["id"]), str(row["text"]))
        for row in rows
        if _blob_to_vec(row["vec"]).size != DIM
    ]
    vectors = await embed_many_async(
        [text] + [item[1] for item in stale]
    )
    replacements = {
        node_id: vector
        for (node_id, _text), vector in zip(stale, vectors[1:])
    }
    return activate(
        conn,
        text,
        cid=cid,
        k=k,
        hops=hops,
        spread=spread,
        min_strength=min_strength,
        query_vec=vectors[0],
        replacement_vectors=replacements,
    )


def remember_episode(
    conn: sqlite3.Connection,
    facts: list[str],
    kind: str = "fact",
    valence: float = 0.0,
    cid: str = "u0",
) -> list[int]:
    """把一句话里拆出的几件事一起存进来，并互相连边（赫布学习）。"""
    ids = [
        add_node(conn, t, kind=kind, valence=valence, cid=cid)
        for t in facts
        if t.strip()
    ]
    if len(ids) > 1:
        coactivate(conn, ids)
    for nid in ids:
        hit(conn, nid, boost=0.05)
    conn.commit()
    return ids


# ---------- 巩固 / 导出 ----------

def consolidate(conn: sqlite3.Connection, threshold: float = 0.55) -> int:
    """短期里反复被激活（strength 被顶到阈值以上）的，标记成长期。

    做法朴素但有效：达到阈值就把 kind 从 event 升为 fact，衰减更慢。
    """
    rows = conn.execute(
        "SELECT id FROM neuron WHERE strength >= ? AND kind = 'event'", (threshold,)
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE neuron SET kind = 'fact' WHERE id = ?", (r["id"],))
    conn.commit()
    return len(rows)


def replay(conn: sqlite3.Connection, picks: int = 8) -> int:
    """做梦：随机挑一批旧记忆，两两连边（记忆重组）。"""
    rows = conn.execute(
        "SELECT id FROM neuron ORDER BY RANDOM() LIMIT ?", (picks,)
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    if len(ids) < 2:
        return 0
    return coactivate(conn, ids, eta=0.03)


def export_graph(conn: sqlite3.Connection, cid: str = "", max_nodes: int = 2000) -> dict:
    """给可视化用：节点 + 边。传 cid 就只看那个人的记忆。"""
    if cid:
        rows = conn.execute(
            "SELECT id, kind, text, strength, valence, hits, created, stability FROM neuron "
            "WHERE cid = ? ORDER BY strength DESC LIMIT ?",
            (cid, max_nodes),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, kind, text, strength, valence, hits, created, stability FROM neuron "
            "ORDER BY strength DESC LIMIT ?",
            (max_nodes,),
        ).fetchall()
    keep = {int(r["id"]) for r in rows}
    nodes = [
        {
            "id": int(r["id"]),
            "label": r["text"][:60],
            "text": r["text"],
            "kind": r["kind"],
            "strength": round(float(r["strength"]), 3),
            "valence": round(float(r["valence"]), 3),
            "hits": int(r["hits"]),
            "stability": round(float(r["stability"]), 2),
            "age_days": round((time.time() - float(r["created"])) / 86400, 1),
        }
        for r in rows
    ]
    links = [
        {
            "source": int(s["a"]),
            "target": int(s["b"]),
            "w": round(float(s["w"]), 3),
        }
        for s in conn.execute("SELECT a, b, w FROM synapse")
        if int(s["a"]) in keep and int(s["b"]) in keep
    ]
    return {"nodes": nodes, "links": links, "built": time.strftime("%Y-%m-%d %H:%M")}


def stats(conn: sqlite3.Connection, cid: str = "") -> dict:
    where, args = ("WHERE cid = ?", (cid,)) if cid else ("", ())
    n = conn.execute(f"SELECT COUNT(*) c FROM neuron {where}", args).fetchone()["c"]
    e = conn.execute("SELECT COUNT(*) c FROM synapse").fetchone()["c"]
    rows = conn.execute(
        f"SELECT kind, COUNT(*) c, AVG(strength) s FROM neuron {where} GROUP BY kind", args
    ).fetchall()
    by_kind = {r["kind"]: {"count": r["c"], "avg_strength": round(r["s"] or 0, 3)} for r in rows}
    top = conn.execute(
        f"SELECT text, strength FROM neuron {where} ORDER BY strength DESC LIMIT 5", args
    ).fetchall()
    return {
        "nodes": n,
        "synapses": e,
        "by_kind": by_kind,
        "strongest": [(r["text"][:30], round(float(r["strength"]), 3)) for r in top],
    }


def update_node(
    conn: sqlite3.Connection,
    node_id: int,
    text: str | None = None,
    kind: str | None = None,
    stability: float | None = None,
    valence: float | None = None,
) -> bool:
    """手改一条记忆。改文字会**重算向量**，否则以后搜不到它。"""
    row = conn.execute("SELECT id, text, kind FROM neuron WHERE id = ?", (node_id,)).fetchone()
    if not row:
        return False
    sets, args = [], []
    if text is not None:
        new_text = text.strip()
        if not new_text:
            return False
        sets.append("text = ?")
        args.append(new_text)
        sets.append("vec = ?")
        args.append(_vec_to_blob(embed(new_text)))
    if kind is not None:
        if kind not in STABILITY:
            return False
        sets.append("kind = ?")
        args.append(kind)
    if stability is not None:
        sets.append("stability = ?")
        args.append(max(0.2, min(MAX_STABILITY, float(stability))))
    if valence is not None:
        sets.append("valence = ?")
        args.append(max(-1.0, min(1.0, float(valence))))
    if not sets:
        return False
    args.append(node_id)
    try:
        conn.execute(f"UPDATE neuron SET {', '.join(sets)} WHERE id = ?", args)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    return True


def delete_node(conn: sqlite3.Connection, node_id: int) -> bool:
    """删掉一条记忆（连带它的突触）。"""
    row = conn.execute("SELECT id FROM neuron WHERE id = ?", (node_id,)).fetchone()
    if not row:
        return False
    conn.execute("DELETE FROM synapse WHERE a = ? OR b = ?", (node_id, node_id))
    conn.execute("DELETE FROM neuron WHERE id = ?", (node_id,))
    conn.commit()
    return True


def add_manual(conn: sqlite3.Connection, text: str, kind: str = "fact", cid: str = "") -> int:
    """手动新增一条记忆（编辑页用）。"""
    return add_node(conn, text.strip(), kind=kind, valence=0.0, cid=cid)
