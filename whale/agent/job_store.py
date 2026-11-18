"""Durable task store for jobs executed by the home worker."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id              TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,
  conversation_id TEXT NOT NULL DEFAULT '',
  payload_json    TEXT NOT NULL,
  payload_hash    TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  attempts        INTEGER NOT NULL DEFAULT 0,
  max_attempts    INTEGER NOT NULL DEFAULT 3,
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL,
  claimed_at      REAL,
  lease_until     REAL,
  finished_at     REAL,
  result_json     TEXT,
  result_hash     TEXT,
  delivered_at    REAL,
  error_code      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""


def _json_hash(value) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class JobStore:
    """Small SQLite task ledger with leases and durable results."""

    def __init__(
        self,
        path: Path | str,
        *,
        max_attempts: int = 3,
        result_max_bytes: int = 16 * 1024 * 1024,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max(1, int(max_attempts))
        self.result_max_bytes = max(1024, int(result_max_bytes))
        self.conn = sqlite3.connect(
            str(self.path), timeout=15, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def enqueue(self, job: dict, *, conversation_id: str = "") -> bool:
        job_id = str(job.get("id") or "")
        if not job_id:
            raise ValueError("job id is required")
        payload = json.dumps(job, ensure_ascii=False, sort_keys=True)
        now = time.time()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO jobs "
            "(id, kind, conversation_id, payload_json, payload_hash, status, "
            "max_attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                job_id,
                str(job.get("kind") or ""),
                str(conversation_id or ""),
                payload,
                _json_hash(job),
                self.max_attempts,
                now,
                now,
            ),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def _recover_expired_locked(self, now: float) -> int:
        changed = 0
        rows = self.conn.execute(
            "SELECT id, attempts, max_attempts FROM jobs "
            "WHERE status = 'claimed' AND lease_until IS NOT NULL "
            "AND lease_until < ?",
            (now,),
        ).fetchall()
        for row in rows:
            if int(row["attempts"]) >= int(row["max_attempts"]):
                status = "unknown"
                error = "lease_expired_max_attempts"
            else:
                status = "pending"
                error = "lease_expired"
            self.conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ?, "
                "claimed_at = NULL, lease_until = NULL, error_code = ? "
                "WHERE id = ?",
                (status, now, error, row["id"]),
            )
            changed += 1
        return changed

    def recover_expired(self) -> int:
        now = time.time()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            changed = self._recover_expired_locked(now)
            self.conn.execute("COMMIT")
            return changed
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def claim_next(self, lease_seconds: int = 120) -> dict | None:
        now = time.time()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._recover_expired_locked(now)
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE status = 'pending' "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                self.conn.execute("COMMIT")
                return None
            self.conn.execute(
                "UPDATE jobs SET status = 'claimed', attempts = attempts + 1, "
                "claimed_at = ?, lease_until = ?, updated_at = ?, "
                "error_code = '' WHERE id = ?",
                (
                    now,
                    now + max(10, int(lease_seconds)),
                    now,
                    row["id"],
                ),
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

        payload = json.loads(row["payload_json"])
        payload["_attempts"] = int(row["attempts"]) + 1
        return payload

    def save_result(self, job_id: str, result) -> bool:
        raw = json.dumps(result, ensure_ascii=False).encode("utf-8")
        now = time.time()
        error_code = ""
        if len(raw) > self.result_max_bytes:
            status = "failed"
            error_code = "result_too_large"
            raw = b"null"
        else:
            status = "result_ready"

        cur = self.conn.execute(
            "UPDATE jobs SET status = ?, result_json = ?, result_hash = ?, "
            "finished_at = ?, updated_at = ?, error_code = ? "
            "WHERE id = ? AND status IN ('pending', 'claimed')",
            (
                status,
                raw.decode("utf-8"),
                hashlib.sha256(raw).hexdigest(),
                now,
                now,
                error_code,
                job_id,
            ),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def get(self, job_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            return {}
        item = dict(row)
        try:
            item["payload"] = json.loads(item.get("payload_json") or "{}")
        except ValueError:
            item["payload"] = {}
        try:
            item["result"] = json.loads(item.get("result_json") or "null")
        except ValueError:
            item["result"] = None
        return item

    def begin_delivery(self, job_id: str) -> bool:
        now = time.time()
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'delivering', updated_at = ? "
            "WHERE id = ? AND status = 'result_ready'",
            (now, job_id),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def mark_consumed(self, job_id: str) -> None:
        now = time.time()
        self.conn.execute(
            "UPDATE jobs SET status = 'consumed', delivered_at = ?, "
            "updated_at = ?, error_code = '' WHERE id = ?",
            (now, now, job_id),
        )
        self.conn.commit()

    def mark_unknown(self, job_id: str, reason: str) -> None:
        now = time.time()
        self.conn.execute(
            "UPDATE jobs SET status = 'unknown', updated_at = ?, "
            "finished_at = ?, error_code = ? WHERE id = ?",
            (now, now, str(reason or "unknown")[:120], job_id),
        )
        self.conn.commit()

    def result_ready(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = 'result_ready' "
            "ORDER BY finished_at ASC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["result"] = json.loads(item.get("result_json") or "null")
            except ValueError:
                item["result"] = None
            out.append(item)
        return out

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def prune(self, keep_days: int = 7) -> int:
        cutoff = time.time() - max(1, keep_days) * 86400
        cur = self.conn.execute(
            "DELETE FROM jobs WHERE updated_at < ? "
            "AND status IN ('consumed', 'failed', 'unknown')",
            (cutoff,),
        )
        self.conn.commit()
        return int(cur.rowcount or 0)
