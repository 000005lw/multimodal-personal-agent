"""SQLite-backed action ledger for the lightweight Agent runtime."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS action_runtime (
  action_id   TEXT PRIMARY KEY,
  cid         TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL,
  arg_hash    TEXT NOT NULL DEFAULT '',
  arg_length  INTEGER NOT NULL DEFAULT 0,
  source      TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'in_flight',
  delivered   INTEGER NOT NULL DEFAULT 0,
  retryable   INTEGER NOT NULL DEFAULT 0,
  attempts    INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  started_at  REAL,
  finished_at REAL,
  error_code  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_action_status ON action_runtime(status);
CREATE INDEX IF NOT EXISTS idx_action_cid ON action_runtime(cid);
"""


class ActionStore:
    """Atomic action state transitions shared across bot restarts."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.path), timeout=15, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def recover_inflight(self) -> int:
        """A previous process died mid-action; do not blindly replay it."""
        now = time.time()
        cur = self.conn.execute(
            "UPDATE action_runtime SET status = 'unknown', delivered = 0, "
            "retryable = 0, updated_at = ?, finished_at = ?, "
            "error_code = 'interrupted_previous_process' "
            "WHERE status = 'in_flight'",
            (now, now),
        )
        self.conn.commit()
        return int(cur.rowcount or 0)

    def begin(
        self,
        action_id: str,
        *,
        cid: str,
        kind: str,
        arg_hash: str,
        arg_length: int,
        source: str,
        retry_failed: bool = False,
    ) -> tuple[str, dict]:
        """Return (decision, record).

        decision is one of started, duplicate_delivered, duplicate_in_flight,
        retry_not_requested, duplicate_terminal.
        """
        now = time.time()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM action_runtime WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO action_runtime "
                    "(action_id, cid, kind, arg_hash, arg_length, source, "
                    "status, attempts, created_at, updated_at, started_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'in_flight', 1, ?, ?, ?)",
                    (
                        action_id,
                        cid,
                        kind,
                        arg_hash,
                        arg_length,
                        source,
                        now,
                        now,
                        now,
                    ),
                )
                decision = "started"
            else:
                status = str(row["status"])
                if status == "delivered":
                    decision = "duplicate_delivered"
                elif status == "in_flight":
                    decision = "duplicate_in_flight"
                elif status == "failed_retryable":
                    decision = "started" if retry_failed else "retry_not_requested"
                else:
                    decision = "duplicate_terminal"

                if decision == "started":
                    self.conn.execute(
                        "UPDATE action_runtime SET cid = ?, kind = ?, "
                        "arg_hash = ?, arg_length = ?, source = ?, "
                        "status = 'in_flight', attempts = attempts + 1, "
                        "updated_at = ?, started_at = ?, finished_at = NULL, "
                        "error_code = '' WHERE action_id = ?",
                        (
                            cid,
                            kind,
                            arg_hash,
                            arg_length,
                            source,
                            now,
                            now,
                            action_id,
                        ),
                    )
            self.conn.execute("COMMIT")
            record = self.conn.execute(
                "SELECT * FROM action_runtime WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            return decision, dict(record) if record else {}
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def finish(
        self,
        action_id: str,
        *,
        status: str,
        delivered: bool,
        retryable: bool,
        error_code: str = "",
    ) -> None:
        now = time.time()
        self.conn.execute(
            "UPDATE action_runtime SET status = ?, delivered = ?, retryable = ?, "
            "updated_at = ?, finished_at = ?, error_code = ? "
            "WHERE action_id = ?",
            (
                status,
                1 if delivered else 0,
                1 if retryable else 0,
                now,
                now,
                str(error_code or "")[:120],
                action_id,
            ),
        )
        self.conn.commit()

    def get(self, action_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM action_runtime WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        return dict(row) if row else {}

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS count FROM action_runtime GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def prune(self, keep_days: int = 30) -> int:
        cutoff = time.time() - max(1, keep_days) * 86400
        cur = self.conn.execute(
            "DELETE FROM action_runtime WHERE updated_at < ? "
            "AND status != 'in_flight'",
            (cutoff,),
        )
        self.conn.commit()
        return int(cur.rowcount or 0)
