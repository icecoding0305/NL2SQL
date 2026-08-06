"""回归评估入口:读取 regression_set.yaml,用 Fake 双打跑完整图并断言期望。

用法:
    python -m nl2sql_agent.eval.run_eval
退出码:0 全部通过,1 存在失败。
"""

from __future__ import annotations

import sys

from langgraph.types import Command

from nl2sql_agent.graph import build_graph
from nl2sql_agent.testing import build_test_deps


def run_case(deps, case: dict) -> tuple[bool, str]:
    graph = build_graph(deps)  # 使用默认 checkpointer(JsonPlusSerializer)
    cfg = {"configurable": {"thread_id": f"eval-{case['id']}"}}
    result = graph.invoke(
        {
            "user_query": case["user_query"],
            "user_id": case["user_id"],
            "data_scope": case["data_scope"],
        },
        cfg,
    )

    # 自动处理候选澄清 / 低置信澄清(选第一个候选 / 继续),保证用例能往下走
    for _ in range(3):
        snap = graph.get_state(cfg)
        if not snap.next:
            break
        nxt = snap.next[0]
        if nxt == "clarify_candidates":
            cands = snap.values.get("retrieval_candidates") or []
            if not cands:
                break
            first = cands[0]
            name = first.table_name if hasattr(first, "table_name") else first.get("table_name")
            result = graph.invoke(Command(resume={"table": name}), cfg)
        elif nxt == "clarify_low_confidence":
            result = graph.invoke(Command(resume={"continue": True}), cfg)
        else:
            break

    expect = case["expect"]
    trace = result.get("trace_steps", [])

    def has(node: str) -> bool:
        return node in trace

    checks: list[tuple[str, bool]] = []

    if expect.get("path") == "simple":
        checks.append(("简单查询也走统一计划路径", has("plan_generation") and has("plan_validation")))
    if expect.get("path") == "complex":
        checks.append(("走计划路径", has("plan_generation") and has("plan_validation")))
    if expect.get("plan_skipped"):
        checks.append(("已生成统一计划", result.get("query_plan") is not None))
    if expect.get("plan_passed"):
        checks.append(("计划通过校验", not result.get("plan_validation_errors")))
    if expect.get("execution_result_nonempty"):
        checks.append(("执行有结果", bool(result.get("execution_result"))))
    if expect.get("schema_no_weilai"):
        tables = [h.table_name for h in result.get("retrieved_schema", [])]
        checks.append(("检索结果不含蔚来表", "store_visit_fact" not in tables))
    if expect.get("sensitive"):
        checks.append(("判定为敏感", bool(result.get("is_sensitive"))))

    if expect.get("paused_at") == "human_review":
        # 首次调用应停在 human_review;人工同意后走完
        snap = graph.get_state(cfg)
        paused = snap.next == ("human_review",) and not snap.values.get("execution_result")
        checks.append(("在 human_review 暂停,未执行", paused))
        if paused:
            graph.invoke(Command(resume={"approved": True}), cfg)
            final = graph.get_state(cfg).values
            checks.append(("人工同意后完成执行", bool(final.get("execution_result"))))

    failed = [name for name, ok in checks if not ok]
    if failed:
        return False, f"未通过: {failed} | trace={trace} | sql={result.get('generated_sql')}"
    return True, f"通过 ({len(checks)} 项断言)"


def main() -> int:
    deps = build_test_deps()
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parent / "regression_set.yaml"
    cases = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("cases", [])
    if not cases:
        print("regression_set.yaml 为空")
        return 1
    failed_count = 0
    for case in cases:
        ok, msg = run_case(deps, case)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {case['id']}: {msg}")
        failed_count += 0 if ok else 1
    print(f"\n合计 {len(cases)} 条,失败 {failed_count} 条")
    return 1 if failed_count else 0


if __name__ == "__main__":
    sys.exit(main())
