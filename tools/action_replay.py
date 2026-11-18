"""Deterministic replay for the lightweight action runtime.

The suite uses only synthetic scenarios. It does not call an LLM, OneBot, or
the real bot process.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import action_protocol  # noqa: E402
import action_runtime  # noqa: E402


SCENARIOS = ROOT / "tests" / "fixtures" / "agent_action_scenarios.jsonl"

CONTEXTS = {
    "owner": {
        "is_owner": True,
        "is_friend": False,
        "is_group": False,
        "synthetic": False,
    },
    "friend": {
        "is_owner": False,
        "is_friend": True,
        "is_group": False,
        "synthetic": False,
    },
    "group": {
        "is_owner": False,
        "is_friend": False,
        "is_group": True,
        "synthetic": False,
    },
    "synthetic": {
        "is_owner": True,
        "is_friend": False,
        "is_group": False,
        "synthetic": True,
    },
}


def load_scenarios(path: Path) -> list[dict]:
    scenarios = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            scenario = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"场景第 {line_number} 行不是合法 JSON：{exc}") from exc
        if "id" not in scenario or "raw" not in scenario or "role" not in scenario:
            raise ValueError(f"场景第 {line_number} 行缺少 id/raw/role")
        scenarios.append(scenario)
    return scenarios


def action_pairs(actions: list[dict]) -> list[list[str]]:
    return [[str(item.get("kind") or ""), str(item.get("arg") or "")] for item in actions]


def replay(path: Path) -> dict:
    scenarios = load_scenarios(path)
    totals = {
        "scenarios": len(scenarios),
        "passed": 0,
        "selection_ok": 0,
        "arguments_ok": 0,
        "execution_ok": 0,
        "unauthorized_executions": 0,
        "duplicate_side_effects": 0,
        "expected_trace_records": 0,
        "actual_trace_records": 0,
    }
    failures = []

    with tempfile.TemporaryDirectory(prefix="whale_action_replay_") as tmp:
        trace_path = Path(tmp) / "trace.jsonl"
        runtime = action_runtime.ActionRuntime(trace_path)

        async def run_all() -> None:
            for scenario in scenarios:
                scenario_id = str(scenario["id"])
                role = str(scenario["role"])
                if role not in CONTEXTS:
                    failures.append(f"{scenario_id}: unknown role {role}")
                    continue
                context = CONTEXTS[role]
                parsed = action_protocol.extract_actions(
                    str(scenario.get("raw") or ""),
                    message_id=scenario_id,
                    allow_actions=bool(scenario.get("allow_actions", True)),
                )
                actual_pairs = action_pairs(parsed["actions"])
                expected_pairs = scenario.get("expected_actions", [])
                kinds_match = [item[0] for item in actual_pairs] == [
                    item[0] for item in expected_pairs
                ]
                args_match = actual_pairs == expected_pairs
                text_match = parsed["text"] == str(scenario.get("expected_text") or "")
                if kinds_match:
                    totals["selection_ok"] += 1
                if args_match:
                    totals["arguments_ok"] += 1

                calls: dict[str, int] = {}

                async def handler(action, current_context):
                    action_id = str(action.get("id") or "")
                    calls[action_id] = calls.get(action_id, 0) + 1
                    allowed, _ = action_runtime.authorize(action, current_context)
                    if not allowed:
                        totals["unauthorized_executions"] += 1
                    return {
                        "status": "success",
                        "delivered": True,
                        "error_code": "",
                    }

                handlers = {
                    "songlist": handler,
                    "introspect": handler,
                    "sticker": handler,
                }

                results = []
                repeat = max(1, int(scenario.get("repeat_dispatch") or 1))
                for action in parsed["actions"]:
                    for _ in range(repeat):
                        results.append(
                            await runtime.dispatch_one(action, context, handlers)
                        )
                for action in parsed["dropped"]:
                    runtime.record_dropped(action, context, "scenario_rewrite")

                executed = [
                    str(result.get("kind") or "")
                    for result in results
                    if result.get("status") == "success"
                ]
                denied = [
                    str(result.get("kind") or "")
                    for result in results
                    if result.get("status") == "denied"
                ]
                unregistered = [
                    str(result.get("kind") or "")
                    for result in results
                    if result.get("error_code") == "unregistered_tool"
                ]
                duplicate_skips = sum(
                    str(result.get("error_code") or "").startswith("duplicate_")
                    or result.get("error_code") == "retry_not_requested"
                    for result in results
                )
                for count in calls.values():
                    if count > 1:
                        totals["duplicate_side_effects"] += count - 1

                expected_executed = scenario.get("expected_executed", [])
                expected_denied = scenario.get("expected_denied", [])
                expected_unregistered = scenario.get("expected_unregistered", [])
                expected_dropped = scenario.get("expected_dropped", [])
                dropped_kinds = [
                    str(item.get("kind") or "") for item in parsed["dropped"]
                ]
                expected_duplicate_skips = int(
                    scenario.get("expected_duplicate_skips") or 0
                )
                totals["expected_trace_records"] += (
                    len(parsed["actions"]) * repeat + len(parsed["dropped"])
                )

                passed = (
                    args_match
                    and text_match
                    and executed == expected_executed
                    and denied == expected_denied
                    and unregistered == expected_unregistered
                    and dropped_kinds == expected_dropped
                    and duplicate_skips == expected_duplicate_skips
                )
                if passed:
                    totals["passed"] += 1
                    totals["execution_ok"] += 1
                else:
                    failures.append(
                        f"{scenario_id}: "
                        f"actions={actual_pairs!r} text={parsed['text']!r} "
                        f"executed={executed!r} denied={denied!r} "
                        f"unregistered={unregistered!r} "
                        f"duplicate_skips={duplicate_skips}"
                    )

        import asyncio
        asyncio.run(run_all())

        if trace_path.exists():
            totals["actual_trace_records"] = len(
                [
                    line for line in trace_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            )

    total = max(1, totals["scenarios"])
    expected_trace = max(1, totals["expected_trace_records"])
    return {
        "scenarios": totals["scenarios"],
        "passed": totals["passed"],
        "selection_accuracy": round(totals["selection_ok"] / total, 4),
        "argument_accuracy": round(totals["arguments_ok"] / total, 4),
        "execution_match_rate": round(totals["execution_ok"] / total, 4),
        "unauthorized_executions": totals["unauthorized_executions"],
        "duplicate_side_effects": totals["duplicate_side_effects"],
        "trace_coverage": round(
            totals["actual_trace_records"] / expected_trace, 4
        ),
        "trace_records": totals["actual_trace_records"],
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    args = parser.parse_args()

    summary = replay(args.scenarios)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print("Agent action replay")
        print("=" * 32)
        print(f"场景：{summary['scenarios']}")
        print(f"通过：{summary['passed']}")
        print(f"工具选择准确率：{summary['selection_accuracy']:.2%}")
        print(f"参数准确率：{summary['argument_accuracy']:.2%}")
        print(f"执行匹配率：{summary['execution_match_rate']:.2%}")
        print(f"未授权执行：{summary['unauthorized_executions']}")
        print(f"重复副作用：{summary['duplicate_side_effects']}")
        print(f"Trace 覆盖：{summary['trace_coverage']:.2%}")
        if summary["failures"]:
            print("\n失败场景：")
            for failure in summary["failures"]:
                print(f"- {failure}")

    failed = summary["passed"] != summary["scenarios"]
    unsafe = (
        summary["unauthorized_executions"] != 0
        or summary["duplicate_side_effects"] != 0
    )
    return 1 if failed or unsafe else 0


if __name__ == "__main__":
    raise SystemExit(main())
