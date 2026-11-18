"""Deterministic fault-oriented checks for memory, actions and durable jobs."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import action_runtime  # noqa: E402
import action_store  # noqa: E402
import bot as botmod  # noqa: E402
import config  # noqa: E402
import job_store  # noqa: E402
import neuron  # noqa: E402


OWNER = {
    "cid": "u10001",
    "is_owner": True,
    "is_friend": False,
    "is_group": False,
    "synthetic": False,
}
GROUP = {
    "cid": "g20002",
    "is_owner": False,
    "is_friend": False,
    "is_group": True,
    "synthetic": False,
}


def _old_neuron_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE neuron (
          id        INTEGER PRIMARY KEY AUTOINCREMENT,
          cid       TEXT    NOT NULL DEFAULT 'u0',
          kind      TEXT    NOT NULL DEFAULT 'fact',
          text      TEXT    NOT NULL UNIQUE,
          vec       BLOB,
          strength  REAL    NOT NULL DEFAULT 1.0,
          stability REAL    NOT NULL DEFAULT 3.0,
          reps      INTEGER NOT NULL DEFAULT 0,
          lapses    INTEGER NOT NULL DEFAULT 0,
          valence   REAL    NOT NULL DEFAULT 0.0,
          created   REAL    NOT NULL,
          last_hit  REAL    NOT NULL,
          hits      INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE synapse (
          a    INTEGER NOT NULL,
          b    INTEGER NOT NULL,
          w    REAL    NOT NULL DEFAULT 0.05,
          kind TEXT    NOT NULL DEFAULT 'cooc',
          PRIMARY KEY (a, b)
        );
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
        """
    )


def check_memory(tmp: Path, results: list[dict]) -> None:
    db = tmp / "old_neuron.db"
    now = time.time()
    conn = sqlite3.connect(str(db))
    _old_neuron_schema(conn)
    vec1 = neuron._vec_to_blob(neuron.hash_embed("喜欢喝茶"))
    vec2 = neuron._vec_to_blob(neuron.hash_embed("住在上海"))
    conn.execute(
        "INSERT INTO neuron "
        "(cid, kind, text, vec, created, last_hit, hits) "
        "VALUES ('uA', 'prefer', '喜欢喝茶', ?, ?, ?, 1)",
        (vec1, now, now),
    )
    conn.execute(
        "INSERT INTO neuron "
        "(cid, kind, text, vec, created, last_hit, hits) "
        "VALUES ('uA', 'fact', '住在上海', ?, ?, ?, 1)",
        (vec2, now, now),
    )
    conn.execute(
        "INSERT INTO synapse (a, b, w, kind) VALUES (1, 2, 0.7, 'cooc')"
    )
    conn.commit()
    conn.close()

    old_key = neuron._KEY
    old_dim = neuron.DIM
    neuron._KEY = ""
    neuron.DIM = neuron.HASH_DIM
    try:
        migrated = neuron.connect(db)
        constraints = neuron._unique_columns(migrated)
        ids = {
            row["id"]
            for row in migrated.execute(
                "SELECT id FROM neuron ORDER BY id"
            ).fetchall()
        }
        edge = migrated.execute(
            "SELECT a, b, w FROM synapse"
        ).fetchone()
        results.append({
            "name": "旧记忆库迁移保留节点和突触",
            "ok": (
                ("cid", "text") in constraints
                and ids == {1, 2}
                and tuple(edge) == (1, 2, 0.7)
            ),
            "detail": f"constraints={constraints}, ids={sorted(ids)}",
        })

        first = neuron.add_node(
            migrated, "喜欢喝茶", kind="prefer", cid="uA"
        )
        second = neuron.add_node(
            migrated, "喜欢喝茶", kind="prefer", cid="uB"
        )
        results.append({
            "name": "不同会话可保存相同文本",
            "ok": first == 1 and second != first,
            "detail": f"A={first}, B={second}",
        })
        count_a = int(
            migrated.execute(
                "SELECT COUNT(*) c FROM neuron WHERE cid='uA' AND text='喜欢喝茶'"
            ).fetchone()["c"]
        )
        count_b = int(
            migrated.execute(
                "SELECT COUNT(*) c FROM neuron WHERE cid='uB' AND text='喜欢喝茶'"
            ).fetchone()["c"]
        )
        results.append({
            "name": "跨会话记忆写入隔离",
            "ok": count_a == 1 and count_b == 1,
            "detail": f"A={count_a}, B={count_b}",
        })
        hits_a = neuron.activate(
            migrated, "喜欢喝茶", cid="uA", k=5
        )
        hits_b = neuron.activate(
            migrated, "喜欢喝茶", cid="uB", k=5
        )
        ids_a = {int(item["id"]) for item in hits_a}
        ids_b = {int(item["id"]) for item in hits_b}
        results.append({
            "name": "检索结果不跨会话混用",
            "ok": ids_a and ids_b and ids_a.isdisjoint(ids_b),
            "detail": f"A={sorted(ids_a)}, B={sorted(ids_b)}",
        })
        migrated.close()
    finally:
        neuron._KEY = old_key
        neuron.DIM = old_dim


def check_action_runtime(tmp: Path, results: list[dict]) -> None:
    ledger_path = tmp / "actions.db"
    first_store = action_store.ActionStore(ledger_path)
    first_runtime = action_runtime.ActionRuntime(
        tmp / "trace.jsonl", ledger=first_store
    )
    calls = {"count": 0}

    async def failing(action, context):
        calls["count"] += 1
        return {
            "status": "failed",
            "retryable": True,
            "delivered": False,
            "error_code": "simulated_retryable",
        }

    async def succeeding(action, context):
        calls["count"] += 1
        return {
            "status": "success",
            "retryable": False,
            "delivered": True,
            "error_code": "",
        }

    action = {
        "id": "m1:sticker:1",
        "kind": "sticker",
        "arg": "开心",
        "source": "test",
    }

    async def run_retry():
        first = await first_runtime.dispatch_one(
            action, OWNER, {"sticker": failing}
        )
        second = await first_runtime.dispatch_one(
            action, OWNER, {"sticker": succeeding}
        )
        return first, second

    first, second = asyncio.run(run_retry())
    results.append({
        "name": "失败动作不提前占用成功结果",
        "ok": (
            first.get("status") == "failed_retryable"
            and second.get("error_code") == "retry_not_requested"
            and calls["count"] == 1
        ),
        "detail": f"first={first.get('status')}, second={second.get('error_code')}",
    })

    retry_runtime = action_runtime.ActionRuntime(
        tmp / "trace.jsonl", ledger=first_store, retry_failed=True
    )

    async def run_explicit_retry():
        return await retry_runtime.dispatch_one(
            action, OWNER, {"sticker": succeeding}
        )

    retried = asyncio.run(run_explicit_retry())
    results.append({
        "name": "可重试失败动作允许显式重试",
        "ok": (
            retried.get("status") == "success"
            and retried.get("delivered") is True
            and calls["count"] == 2
        ),
        "detail": f"status={retried.get('status')}, calls={calls['count']}",
    })

    first_store.close()
    reopened_store = action_store.ActionStore(ledger_path)
    reopened = action_runtime.ActionRuntime(
        tmp / "trace.jsonl", ledger=reopened_store
    )

    async def run_duplicate():
        return await reopened.dispatch_one(
            action, OWNER, {"sticker": succeeding}
        )

    duplicate = asyncio.run(run_duplicate())
    results.append({
        "name": "跨运行时重启的已交付动作不重复执行",
        "ok": (
            duplicate.get("error_code") == "duplicate_delivered"
            and duplicate.get("delivered") is True
            and calls["count"] == 2
        ),
        "detail": f"error={duplicate.get('error_code')}, calls={calls['count']}",
    })

    async def timeout_handler(action, context):
        calls["count"] += 1
        raise TimeoutError("simulated timeout")

    timeout_action = {
        "id": "m2:sticker:1",
        "kind": "sticker",
        "arg": "生气",
        "source": "test",
    }

    timeout_result = asyncio.run(
        reopened.dispatch_one(
            timeout_action, OWNER, {"sticker": timeout_handler}
        )
    )
    results.append({
        "name": "超时动作标记 unknown 而不盲目重试",
        "ok": (
            timeout_result.get("status") == "unknown"
            and timeout_result.get("retryable") is False
        ),
        "detail": (
            f"status={timeout_result.get('status')}, "
            f"retryable={timeout_result.get('retryable')}"
        ),
    })

    free_text_action = {
        "id": "m3:find_image:1",
        "kind": "find_image",
        "arg": "搜索原文",
        "source": "test",
    }
    asyncio.run(
        reopened.dispatch_one(
            free_text_action, OWNER, {"sticker": succeeding}
        )
    )
    trace_text = (tmp / "trace.jsonl").read_text(encoding="utf-8")
    results.append({
        "name": "自由文本动作参数不写入 Trace",
        "ok": "搜索原文" not in trace_text,
        "detail": f"{len(trace_text.splitlines())} traces",
    })
    reopened_store.close()


def check_jobs(tmp: Path, results: list[dict]) -> None:
    path = tmp / "jobs.db"
    store = job_store.JobStore(path, max_attempts=2)
    job = {
        "id": "job-1",
        "kind": "video",
        "url": "https://example.invalid/video",
    }
    inserted = store.enqueue(job, conversation_id="u10001")
    claimed = store.claim_next(lease_seconds=10)
    results.append({
        "name": "任务持久化后可被 Worker 领取",
        "ok": inserted and claimed and claimed.get("id") == "job-1",
        "detail": str(claimed),
    })

    store.conn.execute(
        "UPDATE jobs SET lease_until = ? WHERE id = 'job-1'",
        (time.time() - 1,),
    )
    store.conn.commit()
    store.close()

    reopened = job_store.JobStore(path, max_attempts=2)
    released = reopened.recover_expired()
    reclaimed = reopened.claim_next(lease_seconds=10)
    results.append({
        "name": "服务重启后可恢复过期租约",
        "ok": (
            released == 1
            and reclaimed is not None
            and reclaimed.get("_attempts") == 2
        ),
        "detail": f"released={released}, attempts={reclaimed and reclaimed.get('_attempts')}",
    })

    saved = reopened.save_result("job-1", {"frames": [], "transcript": "ok"})
    duplicate = reopened.save_result("job-1", {"frames": [], "transcript": "ok"})
    results.append({
        "name": "重复结果不会覆盖已完成任务",
        "ok": saved is True and duplicate is False,
        "detail": f"saved={saved}, duplicate={duplicate}",
    })
    begin = reopened.begin_delivery("job-1")
    second_begin = reopened.begin_delivery("job-1")
    reopened.mark_consumed("job-1")
    results.append({
        "name": "结果交付使用独占状态",
        "ok": begin is True and second_begin is False,
        "detail": f"first={begin}, second={second_begin}",
    })
    reopened.close()


def check_async_embedding(tmp: Path, results: list[dict]) -> None:
    old_key = neuron._KEY
    old_cache = neuron._EMB_CACHE
    old_path = neuron._EMB_CACHE_PATH
    old_request = neuron._request_embeddings_async
    flags = {"other_job_ran": False}

    async def fake_request(batch, key, dim):
        await asyncio.sleep(0.03)
        return [[0.25] * dim for _ in batch]

    async def other_job():
        await asyncio.sleep(0.005)
        flags["other_job_ran"] = True

    async def run():
        vectors, _ = await asyncio.gather(
            neuron.embed_many_async(["测试向量", "第二句"]),
            other_job(),
        )
        return vectors

    neuron._KEY = "test-key"
    neuron._EMB_CACHE = {}
    neuron._EMB_CACHE_PATH = tmp / "emb_cache.json"
    neuron._request_embeddings_async = fake_request
    try:
        vectors = asyncio.run(run())
        results.append({
            "name": "异步 Embedding 不阻塞其他协程",
            "ok": (
                flags["other_job_ran"] is True
                and len(vectors) == 2
                and vectors[0].size == neuron.DIM
            ),
            "detail": f"vectors={[v.size for v in vectors]}",
        })
    finally:
        neuron._KEY = old_key
        neuron._EMB_CACHE = old_cache
        neuron._EMB_CACHE_PATH = old_path
        neuron._request_embeddings_async = old_request


def check_security(results: list[dict]) -> None:
    class Request:
        headers = {}
        query = {"token": "query-token"}

    old_token = config.HOME_TOKEN
    config.HOME_TOKEN = "header-only"
    try:
        results.append({
            "name": "relay 不再接受 URL query token",
            "ok": botmod._check_home_token(Request()) is False,
            "detail": "query token rejected",
        })
    finally:
        config.HOME_TOKEN = old_token


def run_checks() -> dict:
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="whale_reliability_") as raw:
        tmp = Path(raw)
        check_memory(tmp, results)
        check_action_runtime(tmp, results)
        check_jobs(tmp, results)
        check_async_embedding(tmp, results)
        check_security(results)
    failed = [item for item in results if not item["ok"]]
    return {
        "checks": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    summary = run_checks()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        for item in summary["results"]:
            mark = "PASS" if item["ok"] else "FAIL"
            print(f"[{mark}] {item['name']}  | {item['detail']}")
        print(
            f"\n{summary['checks']} 项，通过 {summary['passed']} 项，"
            f"失败 {summary['failed']} 项"
        )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
