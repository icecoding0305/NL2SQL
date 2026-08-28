"""用 effective M-Schema 评测表、字段、关系与错误表污染。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from nl2sql_agent.eval.schema_metrics import evaluate_schema_cases
from nl2sql_agent.nodes.m3_schema_retrieval import _expand_query, _hybrid_vector_retrieval
from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.services.deps import CONFIG_DIR, build_vector_store, load_env
from nl2sql_agent.services.schema_catalog import SchemaCatalog
from nl2sql_agent.services.schema_planner import parse_query_intent, rank_field_candidates


def _custom_embed(model_path: str):
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_path)

    def embed(texts: list[str]) -> list[list[float]]:
        return model.encode(texts, normalize_embeddings=True).tolist()

    return embed


def main() -> int:
    parser = argparse.ArgumentParser(description="Schema 混合召回模型基准")
    parser.add_argument("--model-path", help="可选：覆盖默认 Embedding 的本地模型路径")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--column-k", type=int, default=30)
    parser.add_argument("--m-schema", help="可选：显式指定 effective m-schema.json")
    parser.add_argument(
        "--strategy", choices=("legacy", "multipath"), default="multipath"
    )
    parser.add_argument("--json-output", help="可选：写出逐题 JSON 报告")
    parser.add_argument("--min-table-recall", type=float, default=0.0)
    parser.add_argument(
        "--cases",
        default=str(Path(__file__).with_name("schema_retrieval_benchmark.yaml")),
    )
    args = parser.parse_args()

    load_env()
    loader = ConfigLoader(CONFIG_DIR)
    catalog = SchemaCatalog(loader, m_schema_path=args.m_schema)
    if not catalog.metadata.get("m_schema_path"):
        raise RuntimeError(
            "未找到 effective M-Schema；请配置 DATABASE_URL 或传入 --m-schema"
        )
    vector_store = build_vector_store(
        loader,
        catalog,
        embed=_custom_embed(args.model_path) if args.model_path else None,
        cache_signature_override=(
            f"benchmark:{Path(args.model_path).resolve()}" if args.model_path else None
        ),
    )
    rules = loader.load("clarification_rules.yaml").get("clarification_rules", {})
    rules.setdefault("retrieval_confidence", {}).setdefault("multipath", {})[
        "enabled"
    ] = args.strategy == "multipath"
    deps = SimpleNamespace(
        catalog=catalog,
        vector_store=vector_store,
        config=SimpleNamespace(
            schema_search_top_k=args.top_k,
            clarification_rules=rules,
        ),
    )
    cases = (yaml.safe_load(Path(args.cases).read_text(encoding="utf-8")) or {}).get("cases", [])
    evaluated: list[dict] = []
    details: list[dict] = []
    for case in cases:
        query = _expand_query(deps, case["query"])
        intent = parse_query_intent(case["query"])
        ranked, _, slot_scores, slot_field_scores, evidence = _hybrid_vector_retrieval(
            deps,
            query,
            case.get("data_scope", []),
            intent,
            return_evidence=True,
        )
        predicted = [hit.table_name for hit, _ in ranked[:args.top_k]]
        expected = set(case.get("expected_tables", []))
        broad_scores = {hit.table_name: score for hit, score in ranked}
        field_candidates = rank_field_candidates(
            intent,
            catalog.tables_for_scope(case.get("data_scope", [])),
            broad_scores,
            slot_scores,
            slot_field_scores,
        )
        # 按槽位轮转，避免一个槽的多个近分字段占满离线评测列池。
        by_slot: dict[str, list] = {}
        for candidate in field_candidates:
            by_slot.setdefault(candidate.query_slot, []).append(candidate)
        predicted_columns: list[str] = []
        depth = 0
        while len(predicted_columns) < args.column_k and any(
            depth < len(items) for items in by_slot.values()
        ):
            for items in by_slot.values():
                if depth < len(items):
                    item = items[depth]
                    predicted_columns.append(f"{item.table_name}.{item.column_name}")
                    if len(predicted_columns) >= args.column_k:
                        break
            depth += 1
        row = {
            **case,
            "predicted_tables": predicted,
            "predicted_columns": predicted_columns,
            "predicted_joins": [],
        }
        evaluated.append(row)
        recall = (
            len(expected & set(predicted)) / len(expected) if expected else 1.0
        )
        mark = "PASS" if recall == 1.0 else "MISS"
        print(f"[{mark}] {case['query']} expected={sorted(expected)} predicted={predicted}")
        details.append({
            "id": case.get("id"),
            "query": case["query"],
            "predicted_tables": predicted,
            "ranked_table_scores": [
                {"table_name": hit.table_name, "score": round(score, 6)}
                for hit, score in ranked
            ],
            "predicted_columns": predicted_columns,
            "retrieval_evidence": evidence,
        })
    metrics = evaluate_schema_cases(evaluated, table_k=args.top_k)
    print(json.dumps({"strategy": args.strategy, **metrics}, ensure_ascii=False))
    if args.json_output:
        Path(args.json_output).write_text(
            json.dumps(
                {"strategy": args.strategy, "metrics": metrics, "cases": details},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0 if metrics["table_recall_at_k"] >= args.min_table_recall else 1


if __name__ == "__main__":
    raise SystemExit(main())
