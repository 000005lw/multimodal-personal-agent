"""对话记忆：近期逐字窗口 + 滚动摘要 + 完整归档。

三层结构：
  messages —— 最近若干轮，逐字保留，进提示词
  summary  —— 更早内容的摘要，进提示词（比逐字便宜得多）
  archive  —— 全部原始消息，只追加不删除，落盘到 archive.jsonl，永不丢

超出预算时不再直接丢弃，而是把最旧的一段交给模型压成摘要。
"""

import json
import os
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 2


class Memory:
    def __init__(self, path, turns: int = 60, max_chars: int = 20000):
        self.path = Path(path)
        self.turns = max(1, turns)
        self.max_chars = max(2000, max_chars)
        self.archive_path = self.path.parent / "archive.jsonl"
        self._lock = threading.Lock()
        self._data: dict = {"version": SCHEMA_VERSION, "conversations": {}}
        self._load()

    # ---------- 磁盘 ----------
    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._data = data
            except (json.JSONDecodeError, OSError):
                pass  # 文件坏了就重来，不能因此起不来
        self._data.setdefault("conversations", {})
        self._data["version"] = SCHEMA_VERSION

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def _archive(self, cid: str, role: str, content: str) -> None:
        """追加到完整归档。失败也不能影响正常对话，所以吞掉异常。"""
        try:
            self.archive_path.parent.mkdir(parents=True, exist_ok=True)
            record = {"ts": int(time.time()), "cid": cid, "role": role, "content": content}
            with self.archive_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _conv(self, cid: str) -> dict:
        conv = self._data["conversations"].setdefault(cid, {})
        conv.setdefault("messages", [])
        conv.setdefault("notes", [])
        conv.setdefault("summary", "")
        conv.setdefault("archived", 0)
        return conv

    # ---------- 对话历史 ----------
    def history(self, cid: str) -> list[dict]:
        with self._lock:
            return list(self._conv(cid)["messages"])

    def append(self, cid: str, role: str, content: str) -> None:
        with self._lock:
            conv = self._conv(cid)
            conv["messages"].append({"role": role, "content": content})
            self._archive(cid, role, content)
            self._save()

    def needs_compaction(self, cid: str) -> tuple[bool, int, int]:
        """返回 (是否需要压缩, 当前字数, 当前消息条数)。"""
        with self._lock:
            msgs = self._conv(cid)["messages"]
            total = sum(len(m.get("content", "")) for m in msgs)
            over_turns = len(msgs) > self.turns * 2
            return (over_turns or total > self.max_chars), total, len(msgs)

    def peek_old(self, cid: str, ratio: float = 0.5) -> list[dict]:
        """取出最旧的一段（只读不删），交给模型做摘要。"""
        with self._lock:
            msgs = self._conv(cid)["messages"]
            if len(msgs) <= 4:
                return []
            count = max(2, int(len(msgs) * ratio))
            if count % 2:  # 保持问答成对，别把一轮拆开
                count -= 1
            count = min(count, len(msgs) - 2)
            if count <= 0:
                return []
            return [dict(m) for m in msgs[:count]]

    def commit_compaction(self, cid: str, count: int, summary: str) -> None:
        """摘要生成成功后，才真正删除被摘要覆盖的部分。"""
        with self._lock:
            conv = self._conv(cid)
            if count > 0:
                del conv["messages"][:count]
                conv["archived"] = conv.get("archived", 0) + count
            if summary:
                conv["summary"] = summary.strip()
            self._save()

    def summary(self, cid: str) -> str:
        with self._lock:
            return self._conv(cid).get("summary", "")

    def history_for_prompt(self, cid: str, max_chars: int | None = None) -> list[dict]:
        """取历史，从后往前截断到预算内。

        这是最后一层保险：万一压缩没跑成功，也绝不会把超长历史塞给模型把它撑爆。
        """
        budget = max_chars or self.max_chars
        with self._lock:
            msgs = list(self._conv(cid)["messages"])

        kept: list[dict] = []
        total = 0
        for msg in reversed(msgs):
            total += len(msg.get("content", ""))
            if total > budget and kept:
                break
            kept.append(msg)
        kept.reverse()

        # 别让提示词以 assistant 开头，会显得上下文断裂
        while kept and kept[0].get("role") != "user":
            kept.pop(0)
        return kept

    # ---------- 长期备注 ----------
    def notes(self, cid: str) -> list[str]:
        with self._lock:
            return list(self._conv(cid)["notes"])

    def add_note(self, cid: str, note: str) -> bool:
        with self._lock:
            conv = self._conv(cid)
            note = note.strip()
            if not note or note in conv["notes"]:
                return False
            conv["notes"].append(note)
            conv["notes"] = conv["notes"][-80:]
            self._save()
            return True

    def clear_notes(self, cid: str) -> int:
        with self._lock:
            conv = self._conv(cid)
            count = len(conv["notes"])
            conv["notes"] = []
            self._save()
            return count

    # ---------- 清空 ----------
    def reset(self, cid: str) -> None:
        """清空近期窗口和摘要，但保留长期备注。"""
        with self._lock:
            conv = self._conv(cid)
            conv["messages"] = []
            conv["summary"] = ""
            self._save()

    def reset_all(self, cid: str) -> None:
        """连备注一起清掉。"""
        with self._lock:
            conv = self._conv(cid)
            conv["messages"] = []
            conv["summary"] = ""
            conv["notes"] = []
            self._save()

    def stats(self, cid: str) -> dict:
        with self._lock:
            conv = self._conv(cid)
            return {
                "messages": len(conv["messages"]),
                "chars": sum(len(m.get("content", "")) for m in conv["messages"]),
                "archived": conv.get("archived", 0),
                "has_summary": bool(conv.get("summary")),
                "notes": len(conv["notes"]),
            }
