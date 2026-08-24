"""少样本示例库(模块 7 简单路径)。

预留了 add_example —— 反馈闭环(人工确认通过的案例回流)接入点,暂未接入图内。
"""

from __future__ import annotations

import re
from pathlib import Path

from nl2sql_agent.services.config_loader import ConfigLoader


class FewShotStore:
    def __init__(self, loader: ConfigLoader, overlays: dict | None = None):
        path: Path = loader.base_dir / "few_shot.yaml"
        data = loader.load("few_shot.yaml") if path.exists() else {}
        self.version = int(data.get("version", 1))
        self.plan_patterns: list[dict] = [
            item for item in data.get("plan_patterns", []) if item.get("enabled", True)
        ]
        self.sql_examples: list[dict] = [
            item for item in data.get("sql_fallback_examples", [])
            if item.get("enabled", True) and item.get("verified", False)
        ]
        # v1 compatibility: old examples remain usable during gradual migration.
        self.examples: list[dict] = list(data.get("examples", []))
        overlays = overlays or {}
        if overlays.get("plan_patterns"):
            merged = {item.get("id"): item for item in self.plan_patterns}
            merged.update({item.get("id"): item for item in overlays["plan_patterns"]})
            self.plan_patterns = list(merged.values())
        if overlays.get("sql_examples"):
            merged = {item.get("id"): item for item in self.sql_examples}
            merged.update({item.get("id"): item for item in overlays["sql_examples"]})
            self.sql_examples = list(merged.values())

    @staticmethod
    def _configured_features(pattern: dict) -> set[str]:
        skeleton = pattern.get("question_skeleton", {}) or {}
        features: set[str] = set()
        action = skeleton.get("action")
        if action:
            features.add(f"action:{action}")
        for aggregation in skeleton.get("aggregations", []) or []:
            features.add(f"agg:{aggregation}")
        if skeleton.get("group_by"):
            features.add("group")
        if skeleton.get("multi_table"):
            features.add("multi_table")
        if skeleton.get("order_by"):
            features.add("order")
        if skeleton.get("limit"):
            features.add("limit")
        filter_map = {
            "comparison": "filter:compare",
            "equality": "filter:equality",
            "exists": "filter:exists",
            "not_exists": "filter:not_exists",
            "aggregate_comparison": "filter:aggregate_compare",
            "time_range": "time",
        }
        for filter_kind in skeleton.get("filters", []) or []:
            features.add(filter_map.get(str(filter_kind), f"filter:{filter_kind}"))
        return features

    def retrieve(
        self,
        query: str,
        top_k: int = 2,
        *,
        dialect: str | None = None,
        available_tables: set[str] | None = None,
    ) -> list[dict]:
        """Retrieve verified SQL fallbacks bounded to the active query schema."""
        examples = getattr(self, "sql_examples", None) or getattr(self, "examples", [])
        scored = []
        for example in examples:
            example_dialect = str(example.get("dialect") or "").lower()
            if dialect and example_dialect and example_dialect != dialect.lower():
                continue
            used_tables = set(example.get("used_tables", []) or [])
            if available_tables is not None and not used_tables <= available_tables:
                continue
            example_features = self.question_features(example.get("user_query", ""))
            score = self._score(query, example.get("user_query", ""))
            item = dict(example)
            item["retrieval_score"] = round(score, 4)
            item["question_skeleton"] = sorted(example_features)
            item["sql_structure"] = self.sql_structure(example.get("sql", ""))
            scored.append((score, item))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("user_query", ""))))
        return [example for score, example in scored[:top_k] if score > 0]

    @staticmethod
    def question_features(text: str) -> set[str]:
        """Extract schema-independent intent features for skeleton retrieval."""
        normalized = " ".join(str(text or "").lower().split())
        features: set[str] = set()
        rules = {
            "agg:sum": r"累计|合计|总额|总金额|总和|求和|sum",
            "agg:avg": r"平均|均值|avg",
            "agg:count": r"多少|几(个|笔|条)|笔数|数量|count",
            "agg:max": r"最大|最高|max",
            "agg:min": r"最小|最低|min",
            "group": r"每个|各个|按.+(?:统计|汇总|分组)",
            "rank": r"排名|排行|top\s*\d*|前\s*\d+|最高的|最低的",
            "filter:compare": r"超过|大于|高于|不少于|至少|小于|低于|不超过|<=|>=|>|<",
            "filter:equality": r"等于|为|是|=",
            "filter:exists": r"有|存在",
            "filter:not_exists": r"没有|不存在|未发生|从未",
            "detail": r"明细|清单|列表|基本信息|详情|信息|姓名|地址|电话|手机号|编号|证件号",
            "time": r"今天|昨日|本月|上月|今年|去年|近\s*\d+|截至|期间",
        }
        for feature, pattern in rules.items():
            if re.search(pattern, normalized):
                features.add(feature)
        if "rank" in features:
            features.add("action:rank")
        elif any(item.startswith("agg:") for item in features) or "group" in features:
            features.add("action:aggregate")
        elif "detail" in features:
            features.add("action:detail")
        else:
            features.add("action:lookup")
        if "filter:not_exists" in features:
            features.discard("filter:exists")
        if re.search(
            r"关联|同时|分别|以及|且.*(?:客户|产品|贷款|代偿|还款)|"
            r"(?:贷款|代偿|还款|客户|产品).*(?:和|与).*(?:贷款|代偿|还款|客户|产品)",
            normalized,
        ):
            features.add("multi_table")
        if re.search(r"降序|升序|从高到低|从低到高|排序", normalized):
            features.add("order")
        if re.search(r"top\s*\d+|前\s*\d+", normalized):
            features.add("limit")
        if re.search(
            r"(?:累计|合计|总额|平均|数量|笔数).*(?:超过|大于|至少|小于|低于)",
            normalized,
        ):
            features.add("filter:aggregate_compare")
        return features

    @staticmethod
    def sql_structure(sql: str) -> list[str]:
        """Describe SQL shape without exposing example schema identifiers."""
        normalized = " ".join(str(sql or "").upper().split())
        patterns = {
            "select": r"\bSELECT\b", "join": r"\bJOIN\b",
            "where": r"\bWHERE\b", "group_by": r"\bGROUP\s+BY\b",
            "having": r"\bHAVING\b", "order_by": r"\bORDER\s+BY\b",
            "limit": r"\bLIMIT\b", "subquery": r"\(\s*SELECT\b",
            "cte": r"\bWITH\b", "agg:sum": r"\bSUM\s*\(",
            "agg:avg": r"\bAVG\s*\(", "agg:count": r"\bCOUNT\s*\(",
            "agg:max": r"\bMAX\s*\(", "agg:min": r"\bMIN\s*\(",
        }
        return [name for name, pattern in patterns.items() if re.search(pattern, normalized)]

    def _score(self, a: str, b: str) -> float:
        left = self.question_features(a)
        right = self.question_features(b)
        union = left | right
        structural = len(left & right) / len(union) if union else 0.0
        # Lexical similarity only breaks ties. Structural intent dominates, so
        # different business nouns with the same SQL shape can still match.
        a_tokens = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", str(a).lower()))
        b_tokens = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", str(b).lower()))
        lexical = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
        return structural * 0.9 + lexical * 0.1

    def retrieve_patterns(self, query: str, top_k: int = 2) -> list[dict]:
        """Return prompt-safe structural demonstrations, never raw SQL identifiers."""
        patterns = getattr(self, "plan_patterns", [])
        if patterns:
            query_features = self.question_features(query)
            scored = []
            for pattern in patterns:
                configured = self._configured_features(pattern)
                inferred = self.question_features(pattern.get("question_pattern", ""))
                features = configured | inferred
                union = query_features | features
                score = len(query_features & features) / len(union) if union else 0.0
                scored.append((score, pattern, features))
            scored.sort(key=lambda item: (-item[0], str(item[1].get("id", ""))))
            return [
                {
                    "pattern_id": pattern.get("id"),
                    "question_skeleton": sorted(features),
                    "sql_structure": list(
                        (pattern.get("plan_structure", {}) or {}).get("operators", [])
                    ),
                    "plan_structure": pattern.get("plan_structure", {}),
                    "tags": pattern.get("tags", []),
                    "retrieval_score": round(score, 4),
                }
                for score, pattern, features in scored[:top_k]
                if score > 0
            ]
        return [
            {
                "question_skeleton": example["question_skeleton"],
                "sql_structure": example["sql_structure"],
                "tags": example.get("tags", []),
                "retrieval_score": example["retrieval_score"],
            }
            for example in self.retrieve(query, top_k=top_k)
        ]

    def add_example(self, user_query: str, sql: str, used_tables: list[str], tags: list[str]) -> None:
        """反馈闭环写回接口(预留)。"""
        self.examples.append(
            {"user_query": user_query, "sql": sql, "used_tables": used_tables, "tags": tags}
        )
