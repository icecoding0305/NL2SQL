"""用真实 M-Schema 对本地 Embedding 模型运行表召回 Recall@K 基准。"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import yaml

from nl2sql_agent.nodes.m3_schema_retrieval import _expand_query, _hybrid_vector_retrieval
from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.services.deps import CONFIG_DIR, build_vector_store
from nl2sql_agent.services.schema_catalog import SchemaCatalog


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
    parser.add_argument(
        "--cases",
        default=str(Path(__file__).with_name("schema_retrieval_benchmark.yaml")),
    )
    args = parser.parse_args()

    loader = ConfigLoader(CONFIG_DIR)
    catalog = SchemaCatalog(loader)
    vector_store = build_vector_store(
        loader,
        catalog,
        embed=_custom_embed(args.model_path) if args.model_path else None,
        cache_signature_override=(
            f"benchmark:{Path(args.model_path).resolve()}" if args.model_path else None
        ),
    )
    rules = loader.load("clarification_rules.yaml").get("clarification_rules", {})
    deps = SimpleNamespace(
        catalog=catalog,
        vector_store=vector_store,
        config=SimpleNamespace(
            schema_search_top_k=args.top_k,
            clarification_rules=rules,
        ),
    )
    cases = (yaml.safe_load(Path(args.cases).read_text(encoding="utf-8")) or {}).get("cases", [])
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    top1_hits = 0
    for case in cases:
        query = _expand_query(deps, case["query"])
        ranked, _ = _hybrid_vector_retrieval(deps, query, case.get("data_scope", []))
        predicted = [hit.table_name for hit, _ in ranked[:args.top_k]]
        expected = set(case.get("expected_tables", []))
        recall = len(expected & set(predicted)) / len(expected) if expected else 1.0
        recalls.append(recall)
        first_rank = next(
            (index for index, table in enumerate(predicted, start=1) if table in expected),
            None,
        )
        reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
        top1_hits += int(first_rank == 1)
        mark = "PASS" if recall == 1.0 else "MISS"
        print(f"[{mark}] {case['query']} expected={sorted(expected)} predicted={predicted}")
    average = sum(recalls) / len(recalls) if recalls else 0.0
    top1_accuracy = top1_hits / len(cases) if cases else 0.0
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0
    print(
        f"Recall@{args.top_k}={average:.4f} "
        f"Top1Accuracy={top1_accuracy:.4f} MRR={mrr:.4f} cases={len(cases)}"
    )
    return 0 if average == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
