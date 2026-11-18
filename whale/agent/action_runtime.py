"""Small policy and dispatch layer for model-emitted actions."""

from __future__ import annotations

import inspect
import hashlib
import json
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable


# Least privilege first. New tools should be added explicitly.
POLICY: dict[str, set[str]] = {
    "songlist": {"owner_private"},
    "introspect": {"owner_private"},
    "sticker": {"owner_private", "friend_private"},
}


def actor_role(context: dict) -> str:
    if context.get("synthetic"):
        return "synthetic_proactive"
    if context.get("is_group"):
        return "group"
    if context.get("is_owner"):
        return "owner_private"
    if context.get("is_friend"):
        return "friend_private"
    return "unknown"


def authorize(action: dict, context: dict) -> tuple[bool, str]:
    kind = str(action.get("kind") or "")
    allowed_roles = POLICY.get(kind)
    if allowed_roles is None:
        return False, "unregistered_tool"
    role = actor_role(context)
    if role not in allowed_roles:
        return False, f"forbidden_for_{role}"
    return True, "allowed"


class ActionRuntime:
    """Execute registered handlers with role checks and sanitized traces."""

    def __init__(
        self,
        trace_path: Path | str | None = None,
        ledger=None,
        retry_failed: bool = False,
    ):
        self.trace_path = Path(trace_path) if trace_path else None
        self.ledger = ledger
        self.retry_failed = bool(retry_failed)
        self._memory: dict[str, dict] = {}
        self._memory_order: deque[str] = deque(maxlen=2000)

    @staticmethod
    def _trace_arg(action: dict) -> dict:
        """Keep free-form arguments out of traces, retain safe enum values."""
        kind = str(action.get("kind") or "")
        arg = str(action.get("arg") or "")
        safe = {
            "sticker": {
                "开心", "爱心", "摸头", "害羞", "求夸", "感谢", "得意",
                "乖巧", "馋", "好奇", "打招呼", "关注", "鲸鱼", "收到",
                "看书", "思考", "疑惑", "搞事", "搞怪", "无语", "困",
                "生气", "委屈", "难过",
            },
            "introspect": {
                "歌单", "说说", "记忆", "性格", "环境", "日志", "文件",
                "服务", "素材", "全部", "状态", "能力", "数据",
            },
        }
        if kind in safe and arg in safe[kind]:
            return {"arg": arg}
        if kind in {"songlist", "voice", "watch_video", "screenshot"}:
            return {"arg": ""}
        return {
            "arg_hash": hashlib.sha256(arg.encode("utf-8")).hexdigest()[:24],
            "arg_length": len(arg),
        }

    def _begin(self, action: dict, context: dict) -> tuple[str, dict]:
        action_id = str(action.get("id") or "")
        kind = str(action.get("kind") or "")
        arg = str(action.get("arg") or "")
        cid = str(context.get("cid") or "")
        source = str(action.get("source") or "")
        if self.ledger is not None:
            return self.ledger.begin(
                action_id,
                cid=cid,
                kind=kind,
                arg_hash=hashlib.sha256(arg.encode("utf-8")).hexdigest()[:24],
                arg_length=len(arg),
                source=source,
                retry_failed=self.retry_failed,
            )

        row = self._memory.get(action_id)
        if row is None:
            row = {
                "status": "in_flight",
                "delivered": 0,
                "retryable": 0,
                "attempts": 1,
            }
            self._memory[action_id] = row
            if len(self._memory_order) == self._memory_order.maxlen:
                self._memory.pop(self._memory_order[0], None)
            self._memory_order.append(action_id)
            return "started", dict(row)
        status = str(row.get("status") or "")
        if status == "delivered":
            return "duplicate_delivered", dict(row)
        if status == "in_flight":
            return "duplicate_in_flight", dict(row)
        if status == "failed_retryable":
            if not self.retry_failed:
                return "retry_not_requested", dict(row)
            row.update({"status": "in_flight", "retryable": 0})
            row["attempts"] = int(row.get("attempts") or 0) + 1
            return "started", dict(row)
        return "duplicate_terminal", dict(row)

    def _finish(
        self,
        action_id: str,
        *,
        status: str,
        delivered: bool,
        retryable: bool,
        error_code: str,
    ) -> None:
        if self.ledger is not None:
            self.ledger.finish(
                action_id,
                status=status,
                delivered=delivered,
                retryable=retryable,
                error_code=error_code,
            )
            return
        row = self._memory.get(action_id)
        if row is not None:
            row.update(
                {
                    "status": status,
                    "delivered": 1 if delivered else 0,
                    "retryable": 1 if retryable else 0,
                    "error_code": error_code,
                }
            )

    def _trace(self, record: dict) -> None:
        if self.trace_path is None:
            return
        try:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            with self.trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def record_dropped(self, action: dict, context: dict, reason: str) -> None:
        trace_arg = self._trace_arg(action)
        self._trace(
            {
                "ts": round(time.time(), 3),
                "duration_ms": 0,
                "action_id": str(action.get("id") or ""),
                "kind": str(action.get("kind") or ""),
                "source": str(action.get("source") or ""),
                "actor": actor_role(context),
                "status": "dropped",
                "delivered": False,
                "error_code": str(reason or "dropped"),
                **trace_arg,
            }
        )

    async def dispatch_one(
        self,
        action: dict,
        context: dict,
        handlers: dict[
            str,
            Callable[[dict, dict], dict | Awaitable[dict]],
        ],
    ) -> dict:
        started = time.time()
        action_id = str(action.get("id") or "")
        kind = str(action.get("kind") or "")
        role = actor_role(context)
        result = {
            "id": action_id,
            "kind": kind,
            "status": "failed_terminal",
            "delivery_status": "not_attempted",
            "delivered": False,
            "retryable": False,
            "error_code": "",
            "attempt": 0,
        }

        if not action_id:
            result["error_code"] = "missing_action_id"
            self._trace_result(started, action, role, result)
            return result

        decision, record = self._begin(action, context)
        result["attempt"] = int(record.get("attempts") or 0)
        if decision != "started":
            result.update(
                {
                    "status": "skipped",
                    "delivery_status": (
                        "delivered" if bool(record.get("delivered"))
                        else "not_attempted"
                    ),
                    "delivered": bool(record.get("delivered")),
                    "retryable": bool(record.get("retryable")),
                    "error_code": decision,
                }
            )
            self._trace_result(started, action, role, result)
            return result

        if kind not in handlers:
            result.update(
                {
                    "status": "unregistered",
                    "delivery_status": "not_attempted",
                    "error_code": "unregistered_tool",
                }
            )
            self._finish(
                action_id,
                status="unregistered",
                delivered=False,
                retryable=False,
                error_code="unregistered_tool",
            )
        else:
            allowed, reason = authorize(action, context)
            if not allowed:
                result.update({
                    "status": "denied",
                    "delivery_status": "not_attempted",
                    "error_code": reason,
                })
                self._finish(
                    action_id,
                    status="denied",
                    delivered=False,
                    retryable=False,
                    error_code=reason,
                )
            else:
                try:
                    candidate = handlers[kind](action, context)
                    handler_result = (
                        await candidate
                        if inspect.isawaitable(candidate)
                        else candidate
                    )
                    if not isinstance(handler_result, dict):
                        raise TypeError("action handler must return a dict")
                    raw_status = str(handler_result.get("status") or "success")
                    retryable = bool(handler_result.get("retryable"))
                    if raw_status == "skipped":
                        status = "skipped"
                        delivery_status = "not_attempted"
                    elif raw_status in {
                        "failed", "failed_retryable", "failed_terminal"
                    }:
                        status = "failed_retryable" if retryable else "failed_terminal"
                        delivery_status = "not_attempted"
                    elif raw_status == "unknown":
                        status = "unknown"
                        delivery_status = "unknown"
                    elif bool(handler_result.get("delivered")):
                        status = "success"
                        delivery_status = "delivered"
                    else:
                        status = "success"
                        delivery_status = "unconfirmed"
                    result.update(handler_result)
                    result.update(
                        {
                            "status": status,
                            "delivery_status": delivery_status,
                            "delivered": delivery_status == "delivered",
                            "retryable": bool(
                                handler_result.get("retryable")
                                if status.startswith("failed")
                                else False
                            ),
                        }
                    )
                    self._finish(
                        action_id,
                        status=(
                            delivery_status
                            if status == "success"
                            else status
                        ),
                        delivered=result["delivered"],
                        retryable=result["retryable"],
                        error_code=str(handler_result.get("error_code") or ""),
                    )
                except Exception as exc:
                    uncertain = isinstance(exc, (TimeoutError, OSError))
                    status = "unknown" if uncertain else "failed_terminal"
                    result.update(
                        {
                            "status": status,
                            "delivery_status": (
                                "unknown" if status == "unknown"
                                else "not_attempted"
                            ),
                            "delivered": False,
                            "retryable": False,
                            "error_code": type(exc).__name__,
                        }
                    )
                    self._finish(
                        action_id,
                        status=status,
                        delivered=False,
                        retryable=False,
                        error_code=type(exc).__name__,
                    )

        self._trace_result(started, action, role, result)
        return result

    def _trace_result(self, started: float, action: dict, role: str, result: dict) -> None:
        trace_arg = self._trace_arg(action)
        self._trace({
            "ts": round(started, 3),
            "duration_ms": round((time.time() - started) * 1000, 2),
            "action_id": str(action.get("id") or ""),
            "kind": str(action.get("kind") or ""),
            "source": str(action.get("source") or ""),
            "actor": role,
            "status": result.get("status"),
            "delivery_status": result.get("delivery_status"),
            "delivered": bool(result.get("delivered")),
            "retryable": bool(result.get("retryable")),
            "attempt": int(result.get("attempt") or 0),
            "error_code": str(result.get("error_code") or ""),
            **trace_arg,
        })

    async def dispatch(
        self,
        actions: list[dict],
        context: dict,
        handlers: dict[
            str,
            Callable[[dict, dict], dict | Awaitable[dict]],
        ],
    ) -> list[dict]:
        results = []
        for action in actions:
            results.append(await self.dispatch_one(action, context, handlers))
        return results
