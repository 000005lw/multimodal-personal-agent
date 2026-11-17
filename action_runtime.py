"""Small policy and dispatch layer for model-emitted actions."""

from __future__ import annotations

import inspect
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

    def __init__(self, trace_path: Path | str | None = None):
        self.trace_path = Path(trace_path) if trace_path else None
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=2000)

    def _record_once(self, action_id: str) -> bool:
        if action_id in self._seen:
            return False
        if len(self._seen_order) == self._seen_order.maxlen:
            self._seen.discard(self._seen_order[0])
        self._seen.add(action_id)
        self._seen_order.append(action_id)
        return True

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
        self._trace(
            {
                "ts": round(time.time(), 3),
                "duration_ms": 0,
                "action_id": str(action.get("id") or ""),
                "kind": str(action.get("kind") or ""),
                "arg": str(action.get("arg") or "")[:80],
                "source": str(action.get("source") or ""),
                "actor": actor_role(context),
                "status": "dropped",
                "delivered": False,
                "error_code": str(reason or "dropped"),
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
            "status": "failed",
            "delivered": False,
            "error_code": "",
        }

        if not self._record_once(action_id):
            result.update({"status": "skipped", "error_code": "duplicate_action_id"})
        elif kind not in handlers:
            result["error_code"] = "unregistered_tool"
        else:
            allowed, reason = authorize(action, context)
            if not allowed:
                result.update({"status": "denied", "error_code": reason})
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
                    result.update(handler_result)
                    result.setdefault("status", "success")
                    result["delivered"] = bool(result.get("delivered"))
                except Exception as exc:
                    result.update(
                        {
                            "status": "failed",
                            "error_code": type(exc).__name__,
                        }
                    )

        self._trace(
            {
                "ts": round(started, 3),
                "duration_ms": round((time.time() - started) * 1000, 2),
                "action_id": action_id,
                "kind": kind,
                "arg": str(action.get("arg") or "")[:80],
                "source": str(action.get("source") or ""),
                "actor": role,
                "status": result.get("status"),
                "delivered": bool(result.get("delivered")),
                "error_code": str(result.get("error_code") or ""),
            }
        )
        return result

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
