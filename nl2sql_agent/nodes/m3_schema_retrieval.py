"""模块 3:Schema 检索(准确率的地基)。

混合检索:
1. 先查术语映射表(按 data_scope 确定的业务线命名空间 → 查不到再查全局)
2. 表级与字段级向量并行召回并融合评分
3. 用关系向量补充与主候选直接关联的 Join 表

产出:
- retrieved_schema:命中的表
- retrieval_confidence ∈ [0,1]:术语精确命中为 1.0,向量兜底为归一化相似度
- retrieval_candidates:分数与最佳候选接近(差值 < candidate_gap_threshold)的候选,
  供模块 3.5 判断是否需要候选澄清

关键:检索阶段必须按 state.data_scope 过滤,无权限的表绝不进入 retrieved_schema,
不指望后面节点补权限。该用的字段没检索到,后面无论生成还是校验都救不回来。
"""

from __future__ import annotations

import re

from nl2sql_agent.services.schema_planner import (
    build_schema_plan,
    extend_schema_plan_for_output_bindings,
    find_field_ambiguities,
    ground_output_bindings,
    parse_query_intent,
    plan_table_names,
    prefer_coherent_field_bindings,
    prefer_minimal_table_cover,
    prefer_primary_fact_fields,
    rank_field_candidates,
    resolve_anchor_table_ambiguities,
)
from nl2sql_agent.services.projection_resolver import (
    materialize_projection_decision,
    resolve_vague_projection,
)
from nl2sql_agent.services.semantic_parser import iter_semantic_atoms
from nl2sql_agent.services.semantic_coverage import refresh_semantic_coverage
from nl2sql_agent.services.semantic_query import metric_semantics
from nl2sql_agent.services.value_grounding import ground_text_binding
from nl2sql_agent.state import (
    BusinessClarification,
    BusinessClarificationOption,
    DecisionSource,
    DecisionSummary,
    NL2SQLState,
    SchemaHit,
)

COLLECTION_COLUMN = "schema_column"
COLLECTION_RELATION = "schema_relation"


def _dedupe(hits: list[SchemaHit]) -> list[SchemaHit]:
    seen: set[str] = set()
    out: list[SchemaHit] = []
    for h in hits:
        if h.table_name not in seen:
            seen.add(h.table_name)
            out.append(h)
    return out


def _effective_relations(deps, base_relations: list[dict] | None = None) -> list[dict]:
    """Merge generated FK facts with user-verified database relationship overlays."""
    merged: dict[tuple, dict] = {}
    for relation in [
        *(base_relations or []),
        *getattr(deps.catalog, "relation_overrides", []),
    ]:
        key = (
            relation.get("source_table"),
            tuple(relation.get("source_columns") or []),
            relation.get("target_table"),
            tuple(relation.get("target_columns") or []),
        )
        if all((key[0], key[1], key[2], key[3])):
            merged[key] = dict(relation)
    return list(merged.values())


def _business_clarification(deps, scope, field_ambiguities):
    """把物理字段近分转换为业务语言选项；绑定只保留在后端 state。"""
    if not field_ambiguities:
        return None, {}
    slot, candidates = next(iter(field_ambiguities.items()))
    table_map = {table.name: table for table in deps.catalog.tables_for_scope(scope)}
    options: list[BusinessClarificationOption] = []
    bindings: dict[str, str] = {}
    used_labels: set[str] = set()
    for index, candidate in enumerate(candidates, start=1):
        label = ""
        table = table_map.get(candidate.table_name)
        if table is not None:
            column = next(
                (item for item in table.columns if item.get("name") == candidate.column_name),
                None,
            )
            label = str((column or {}).get("comment") or "").strip()
        if not label:
            # evidence 是业务可读摘要，不包含物理绑定；字段名不能作为兜底标签。
            label = next((item for item in candidate.evidence if item), "")
        if not label or label in used_labels:
            label = f"{slot}口径{index}"
        used_labels.add(label)
        option_id = f"business_option_{index}"
        options.append(BusinessClarificationOption(
            id=option_id,
            label=label,
            description="用于确认同一业务概念的统计口径",
        ))
        bindings[option_id] = f"{candidate.table_name}.{candidate.column_name}"
    return BusinessClarification(
        slot=slot,
        question=f"“{slot}”存在多种业务口径，请确认您需要哪一种",
        options=options,
    ), bindings


def _retain_true_business_ambiguities(deps, scope, field_ambiguities):
    """Drop choices whose public business labels are identical.

    Identical labels represent physical placement ambiguity, which must be
    resolved by anchor/relationship planning rather than delegated to users.
    """
    if not field_ambiguities:
        return {}
    table_map = {table.name: table for table in deps.catalog.tables_for_scope(scope)}
    retained = {}
    for slot, candidates in field_ambiguities.items():
        labels: set[str] = set()
        for candidate in candidates:
            table = table_map.get(candidate.table_name)
            column = next((
                item for item in (table.columns if table is not None else [])
                if item.get("name") == candidate.column_name
            ), None)
            label = re.sub(r"\s+", "", str((column or {}).get("comment") or ""))
            if label:
                labels.add(label)
        if len(labels) > 1:
            retained[slot] = candidates
    return retained


def _enrich_decision_summary(
    state, deps, schema_plan, confidence, projection_decision=None
):
    base = state.decision_summary or DecisionSummary(
        understood_query=state.clarified_query or state.user_query
    )
    sources: list[DecisionSource] = []
    if schema_plan is not None:
        visible = {table.name: table for table in deps.catalog.tables_for_scope(state.data_scope)}
        for planned in [
            *schema_plan.anchor_tables,
            *schema_plan.dimension_tables,
            *schema_plan.bridge_tables,
        ]:
            table = visible.get(planned.table_name)
            business_name = str(getattr(table, "comment", "") or "业务数据").strip()
            sources.append(DecisionSource(
                business_name=business_name,
                role={
                    "primary_fact": "核心事实",
                    "secondary_fact": "补充事实",
                    "entity": "实体信息",
                    "dimension": "分析维度",
                    "bridge": "关联桥接",
                }.get(planned.role, "业务数据"),
                reason=planned.reason,
            ))
    warnings = list(base.warnings)
    for slot in (schema_plan.unresolved_slots if schema_plan is not None else []):
        message = f"Schema 证据不足：{slot}"
        if message not in warnings:
            warnings.append(message)
    business_steps = list(base.business_steps)
    resolved_outputs = list(base.resolved_outputs)
    excluded_outputs = list(base.excluded_outputs)
    missing_outputs = list(base.missing_outputs)
    if projection_decision is not None:
        labels = [item.business_label for item in projection_decision.selected_fields]
        if labels:
            business_steps.append(
                f"将“{projection_decision.request}”具体化为：{'、'.join(labels)}"
            )
            resolved_outputs.extend(labels)
        excluded_outputs.extend(
            f"{item.business_label}：{item.reason}"
            for item in projection_decision.excluded_fields
        )
        missing_outputs.extend(projection_decision.missing_concepts)
    return base.model_copy(update={
        "data_sources": sources,
        "business_steps": list(dict.fromkeys(business_steps)),
        "confidence": {**base.confidence, "schema_planning": float(confidence)},
        "warnings": warnings,
        "resolved_outputs": list(dict.fromkeys(resolved_outputs)),
        "excluded_outputs": list(dict.fromkeys(excluded_outputs)),
        "missing_outputs": list(dict.fromkeys(missing_outputs)),
    })


def _runtime_value_lookup(deps):
    """Build a bounded, parameterized lookup for non-sensitive enum values."""
    quote = "`" if str(deps.config.dialect).lower() == "mysql" else '"'

    def lookup(candidate, column, value: str) -> list[str]:
        table_name = candidate.table_name
        column_name = candidate.column_name
        if not all(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item)
            for item in (table_name, column_name)
        ):
            return []
        table = f"{quote}{table_name}{quote}"
        field = f"{quote}{column_name}{quote}"
        sql = (
            f"SELECT DISTINCT {field} AS matched_value FROM {table} "
            f"WHERE {field} LIKE %s LIMIT 5"
        )
        try:
            rows = deps.executor.execute(
                sql, timeout_seconds=3, params=(f"%{value}%",)
            )
        except Exception:  # 值域探测失败时安全降级到离线画像
            return []
        return [
            str(row.get("matched_value"))
            for row in rows
            if row.get("matched_value") is not None
        ]

    return lookup


def _ground_semantic_atoms(
    state, candidates, tables, deps=None, allowed_tables: set[str] | None = None,
):
    """将语义原子绑定到已评分物理字段，形成可确定性校验的 Grounding 证据。"""
    bindings: dict[str, dict] = {}
    if state.semantic_graph is None:
        return bindings
    for atom in iter_semantic_atoms(state.semantic_graph.predicate):
        if atom.predicate_type not in {"comparison", "status", "aggregate_comparison"}:
            continue
        slot = atom.grounding_concept or atom.concept
        options = [item for item in candidates if item.query_slot == slot]
        if allowed_tables:
            options = [item for item in options if item.table_name in allowed_tables]
        override = state.selected_field_overrides.get(slot)
        if override:
            options = [
                item for item in options
                if f"{item.table_name}.{item.column_name}" == override
            ]
        if not options:
            continue
        selected = options[0]
        operator = atom.operator
        value = atom.value
        value_evidence: list[str] = []
        if isinstance(value, str) and str(operator or "=").lower() == "=":
            grounded = ground_text_binding(
                value,
                options,
                tables,
                value_lookup=_runtime_value_lookup(deps) if deps is not None else None,
            )
            if grounded is not None:
                selected, operator, value, value_evidence = grounded
        bindings[atom.atom_id] = {
            "table_name": selected.table_name,
            "column_name": selected.column_name,
            "operator": operator,
            "value": value,
            "confidence": selected.final_score,
            "evidence": list(dict.fromkeys([*selected.evidence, *value_evidence])),
            "aggregation": (
                metric_semantics(atom.source_text or atom.concept)[1]
                if atom.predicate_type == "aggregate_comparison" else None
            ),
            "scope": atom.scope,
        }
    return bindings


def _unsupported_output_concepts(graph, bindings: dict[str, dict]) -> list[str]:
    return [
        output.concept
        for output in (graph.outputs if graph else [])
        if output.required and output.id not in bindings
    ]


def _unsupported_output_answer(concepts: list[str]) -> str:
    joined = "、".join(concepts)
    return (
        f"当前可访问的数据结构中无法确认以下返回字段：{joined}。"
        "系统已停止生成 SQL，以避免猜测字段或返回不完整结果。"
    )


def _candidate_is_close(deps, top_score: float, score: float) -> bool:
    rc = deps.config.clarification_rules.get("retrieval_confidence", {})
    ratio = rc.get("candidate_gap_ratio")
    if ratio is not None and top_score > 0:
        return (top_score - score) / top_score < float(ratio)
    return top_score - score < float(rc.get("candidate_gap_threshold", 0.1))


def _supplement_config(deps) -> tuple[int, float, float]:
    rc = deps.config.clarification_rules.get("retrieval_confidence", {})
    return (
        int(rc.get("supplement_top_n", 3)),
        float(rc.get("supplement_threshold", 0.1)),
        float(rc.get("field_relevance_weight", 0.08)),
    )


def _hybrid_config(deps, query: str = "") -> tuple[float, float, int, float]:
    rc = deps.config.clarification_rules.get("retrieval_confidence", {})
    field_query = any(marker in query for marker in rc.get("field_query_markers", []))
    return (
        float(rc.get("field_query_table_weight", 0.4) if field_query else rc.get("table_vector_weight", 0.65)),
        float(rc.get("field_query_column_weight", 0.6) if field_query else rc.get("column_vector_weight", 0.35)),
        int(rc.get("relation_expand_top_n", 2)),
        float(rc.get("relation_threshold", 0.1)),
    )


def _expand_query(deps, query: str) -> str:
    rc = deps.config.clarification_rules.get("retrieval_confidence", {})
    additions: list[str] = []
    for phrase, synonyms in rc.get("query_expansions", {}).items():
        if phrase in query:
            additions.extend(str(value) for value in synonyms)
    return query if not additions else f"{query} {' '.join(dict.fromkeys(additions))}"


def _search_collection(deps, collection: str, query: str, scope: list[str], top_k: int) -> list[dict]:
    """按多个业务线分别检索，并按文档 id 去重保留最高分。"""
    by_id: dict[str, dict] = {}
    for business_line in scope:
        for item in deps.vector_store.search(
            collection, query, top_k, {"business_line": business_line}
        ):
            current = by_id.get(item["id"])
            if current is None or item["score"] > current["score"]:
                by_id[item["id"]] = item
    return sorted(by_id.values(), key=lambda item: -item["score"])


def _catalog_hits(deps, scope: list[str]) -> dict[str, SchemaHit]:
    return {
        table.name: SchemaHit(
            table_name=table.name,
            columns=[dict(column) for column in table.columns],
            business_terms=[],
        )
        for table in deps.catalog.tables_for_scope(scope)
    }


def _hybrid_vector_retrieval(deps, query: str, scope: list[str]) -> tuple[list[tuple[SchemaHit, float]], list[dict]]:
    """融合表级和字段级分数，并返回可用于 Join 扩展的关系召回结果。"""
    top_k = deps.config.schema_search_top_k
    table_results = deps.vector_store.search_scored(query, top_k=top_k, data_scope=scope)
    column_results = _search_collection(deps, COLLECTION_COLUMN, query, scope, top_k * 3)
    relation_results = _search_collection(deps, COLLECTION_RELATION, query, scope, top_k * 3)
    available = _catalog_hits(deps, scope)

    table_scores = {hit.table_name: score for hit, score in table_results}
    column_scores: dict[str, float] = {}
    for item in column_results:
        table_name = str(item.get("metadata", {}).get("table_name") or "")
        if table_name in available:
            column_scores[table_name] = max(column_scores.get(table_name, 0.0), item["score"])

    table_weight, column_weight, _, _ = _hybrid_config(deps, query)
    ranked: list[tuple[SchemaHit, float]] = []
    for table_name in table_scores.keys() | column_scores.keys():
        table_score = table_scores.get(table_name)
        column_score = column_scores.get(table_name)
        if table_score is not None and column_score is not None:
            weight_sum = table_weight + column_weight
            score = (
                table_weight * table_score + column_weight * column_score
            ) / weight_sum if weight_sum else max(table_score, column_score)
        else:
            # 只有一个索引有结果时不人为压低分数，兼容尚未生成字段向量的旧索引。
            score = table_score if table_score is not None else column_score
        if table_name in available and score is not None:
            ranked.append((available[table_name], min(1.0, score)))
    ranked.sort(key=lambda pair: -pair[1])
    return ranked[:top_k], relation_results


def _relation_supplements(
    deps,
    relation_results: list[dict],
    seed_names: set[str],
    scope: list[str],
) -> list[SchemaHit]:
    """只扩展与已召回主表直接相连且当前用户可见的关系表。"""
    available = _catalog_hits(deps, scope)
    _, _, top_n, threshold = _hybrid_config(deps)
    out: list[SchemaHit] = []
    seen = set(seed_names)
    for item in relation_results:
        if item["score"] < threshold:
            continue
        metadata = item.get("metadata", {})
        source = str(metadata.get("table_name") or "")
        target = str(metadata.get("target_table") or "")
        candidate = target if source in seed_names else source if target in seed_names else ""
        if candidate and candidate in available and candidate not in seen:
            out.append(available[candidate])
            seen.add(candidate)
            if len(out) >= top_n:
                break
    return out


def _join_path_supplements(deps, seed_names: set[str], scope: list[str]) -> list[SchemaHit]:
    """在语义候选之间寻找最短 FK 路径，只返回必需的桥接表。"""
    if len(seed_names) < 2:
        return []
    from collections import deque

    from nl2sql_agent.services.schema_ingest.text_builder import load_mschema_vector_source

    source = load_mschema_vector_source(getattr(deps.catalog, "metadata", {}))
    if source is None:
        return []
    mschema, _ = source
    available = _catalog_hits(deps, scope)
    graph: dict[str, set[str]] = {}
    for relation in _effective_relations(deps, mschema.get("relations", [])):
        left = str(relation.get("source_table") or "")
        right = str(relation.get("target_table") or "")
        if left in available and right in available:
            graph.setdefault(left, set()).add(right)
            graph.setdefault(right, set()).add(left)

    max_hops = int(
        deps.config.clarification_rules.get("retrieval_confidence", {}).get("max_join_path_hops", 3)
    )
    bridges: list[SchemaHit] = []
    seen_bridges: set[str] = set()
    ordered_seeds = sorted(seed_names)
    for index, start in enumerate(ordered_seeds):
        for target in ordered_seeds[index + 1:]:
            queue = deque([(start, [start])])
            visited = {start}
            path: list[str] = []
            while queue:
                node, current_path = queue.popleft()
                if len(current_path) - 1 >= max_hops:
                    continue
                for neighbor in graph.get(node, set()):
                    if neighbor in visited:
                        continue
                    candidate_path = [*current_path, neighbor]
                    if neighbor == target:
                        path = candidate_path
                        queue.clear()
                        break
                    visited.add(neighbor)
                    queue.append((neighbor, candidate_path))
            for table_name in path[1:-1]:
                if table_name not in seed_names and table_name not in seen_bridges:
                    bridges.append(available[table_name])
                    seen_bridges.add(table_name)
    return bridges


def _field_relevance(query: str, matched_terms: list[str], hit: SchemaHit) -> int:
    """查询剩余词(去掉已命中术语)与表字段名/注释的重叠次数。

    用于给"语义相关的关联表"加分:如"学历"命中客户表字段(HIGHEST_SCHOOLING 注释"最高学历"),
    而无关表(还款明细)没有学历字段 → 学历查询时客户表被优先补充。
    """
    text = query
    for t in matched_terms:
        text = text.replace(t, " ")
    field_text = " ".join(
        str(c.get("name", "")) + " " + str(c.get("comment", "")) for c in hit.columns
    )
    hits = 0
    for i in range(len(text) - 1):
        bigram = text[i : i + 2]
        if bigram.strip() and bigram in field_text:
            hits += 1
    return hits


def make_schema_retrieval_node(deps):
    def schema_retrieval_node(state: NL2SQLState) -> NL2SQLState | dict:
        # 候选澄清已解决(用户选定后回检索):保留现有结果,不再重算,避免重复触发澄清
        if state.retrieval_resolved:
            return {}

        original_query = state.clarified_query or state.user_query
        query = _expand_query(deps, original_query)
        scope = state.data_scope
        visible_tables = list(deps.catalog.tables_for_scope(scope))
        # 正常图链路只消费模块2已冻结的语义派生 QueryIntent，禁止再次解析覆盖。
        # 仅保留直接单测/旧调用方未经过模块2时的兼容降级。
        query_intent = state.query_intent or parse_query_intent(original_query)
        field_candidates = []
        field_ambiguities = {}
        schema_plan = None

        # 第一层:术语映射(业务线命名空间 → 全局兜底)
        hits: list[SchemaHit] = []
        matched_terms: list[str] = []
        has_required_multi_table_hit = False
        has_composite_term = False
        for term in deps.term_mapping.extract_terms(query, scope):
            res = deps.term_mapping.resolve(term, scope)
            if res.status.value == "found":
                entry = res.entries[0]
                has_composite_term = has_composite_term or bool(entry.composite_metric)
                matched_terms.append(entry.term)
                term_hits = deps.catalog.hits_for_term(entry.term, entry.resolved_fields, scope)
                if not term_hits:
                    term_hits = deps.catalog.hits_covering_term_fields(
                        entry.term, entry.resolved_fields, scope
                    )
                    has_required_multi_table_hit = len(term_hits) > 1
                hits.extend(term_hits)
            # ambiguous/not_found 不参与检索,交给向量兜底

        # 第二层:表级 + 字段级混合向量检索；第三层关系向量仅用于受约束补表。
        scored, relation_results = _hybrid_vector_retrieval(deps, query, scope)
        structured_intent = bool(
            query_intent.measures
            or query_intent.filters
            or (query_intent.entities and query_intent.attributes)
            or query_intent.dimensions
            or query_intent.query_type == "existence"
        )
        # 普通字段映射不能截断一个多槽位问题；复合指标或无法结构化的问题仍保留
        # 原术语优先链路。这样“贷款金额和还款金额”不会只因命中前者而漏掉后者。
        all_measures_mapped = bool(query_intent.measures) and all(
            any(measure.text in term or term in measure.text for term in matched_terms)
            for measure in query_intent.measures
        )
        standalone_mapped_measure = bool(
            all_measures_mapped
            and not query_intent.entities
            and not query_intent.attributes
            and not query_intent.dimensions
        )
        use_term_hits = bool(
            hits and (has_composite_term or not structured_intent or standalone_mapped_measure)
        )
        if use_term_hits:
            # 有术语命中:补充关联表(解决多表 join 查询缺表)。
            # 综合分 = 向量分 + 字段相关加成:查询剩余词(去掉已命中术语)命中表字段的
            # 表优先补充,避免"学历"这类过滤维度被无关表(如还款明细)挤掉。
            supp_top_n, supp_min, field_weight = _supplement_config(deps)
            hit_names = {h.table_name for h in hits}
            scored2 = [
                (h, c + field_weight * _field_relevance(query, matched_terms, h))
                for h, c in scored
                if h.table_name not in hit_names
            ]
            scored2 = [(h, s) for h, s in scored2 if s >= supp_min]
            if scored2:
                relative = float(
                    deps.config.clarification_rules.get("retrieval_confidence", {})
                    .get("supplement_relative_threshold", 0.45)
                )
                adaptive_min = max(supp_min, scored2[0][1] * relative)
                scored2 = [(h, s) for h, s in scored2 if s >= adaptive_min]
            scored2.sort(key=lambda x: -x[1])
            supplement = [h for h, _ in scored2[:supp_top_n]]
            relation_supplement = _relation_supplements(
                deps, relation_results, hit_names, scope
            )
            path_supplement = _join_path_supplements(
                deps,
                hit_names | {hit.table_name for hit in supplement},
                scope,
            )
            merged = _dedupe(hits + supplement + relation_supplement + path_supplement)
        else:
            # 普通字段问题优先走“字段证据 → 锚点/实体角色 → 最小关系子图”。
            # 无法抽取结构时才回退旧 Top-K 向量链路，避免把渐进改造变成破坏性切换。
            if structured_intent:
                from nl2sql_agent.services.schema_ingest.text_builder import (
                    load_mschema_vector_source,
                )

                score_by_table = {hit.table_name: score for hit, score in scored}
                field_candidates = rank_field_candidates(
                    query_intent, visible_tables, score_by_table
                )
                field_candidates = prefer_minimal_table_cover(
                    field_candidates, query_intent
                )
                field_ambiguities = find_field_ambiguities(
                    field_candidates, state.selected_field_overrides
                )
                # 仅作为结果投影的属性允许自动选择/展开，不要求用户确认。
                # 同名槽位若同时参与筛选、分组或度量，仍保留唯一口径确认。
                constrained_slots = {
                    item.text for item in [
                        *query_intent.filters,
                        *query_intent.dimensions,
                        *query_intent.measures,
                    ]
                }
                output_only_slots = {
                    item.text for item in query_intent.attributes
                    if item.text not in constrained_slots
                }
                # Grouping by an entity (for example “每个客户/产品”) is a
                # physical identity decision. Resolve it from keys and the
                # relation graph internally instead of asking users to choose
                # between customer status, category and identifier columns.
                entity_identity_slots = {
                    item for item in (
                        state.semantic_graph.group_by if state.semantic_graph else []
                    )
                }
                field_ambiguities = {
                    slot: options for slot, options in field_ambiguities.items()
                    if slot not in output_only_slots and slot not in entity_identity_slots
                }
                source = load_mschema_vector_source(getattr(deps.catalog, "metadata", {}))
                relations = _effective_relations(
                    deps,
                    source[0].get("relations", []) if source is not None else [],
                )
                max_hops = int(
                    deps.config.clarification_rules.get("retrieval_confidence", {})
                    .get("max_join_path_hops", 3)
                )
                field_candidates = prefer_coherent_field_bindings(
                    field_candidates,
                    query_intent,
                    visible_tables,
                    relations,
                    state.selected_field_overrides,
                    max_hops=max_hops,
                )
                field_ambiguities = find_field_ambiguities(
                    field_candidates, state.selected_field_overrides
                )
                field_ambiguities = {
                    slot: options for slot, options in field_ambiguities.items()
                    if slot not in output_only_slots and slot not in entity_identity_slots
                }
                schema_plan = build_schema_plan(
                    query_intent,
                    visible_tables,
                    field_candidates,
                    relations,
                    overrides=state.selected_field_overrides,
                    max_hops=max_hops,
                )
                reranked_candidates = prefer_primary_fact_fields(
                    field_candidates, schema_plan
                )
                reranked_candidates = prefer_minimal_table_cover(
                    reranked_candidates, query_intent
                )
                reranked_candidates = prefer_coherent_field_bindings(
                    reranked_candidates,
                    query_intent,
                    visible_tables,
                    relations,
                    state.selected_field_overrides,
                    max_hops=max_hops,
                )
                if reranked_candidates != field_candidates:
                    field_candidates = reranked_candidates
                    field_ambiguities = find_field_ambiguities(
                        field_candidates, state.selected_field_overrides
                    )
                    schema_plan = build_schema_plan(
                        query_intent,
                        visible_tables,
                        field_candidates,
                        relations,
                        overrides=state.selected_field_overrides,
                        max_hops=max_hops,
                    )
                field_ambiguities = resolve_anchor_table_ambiguities(
                    field_ambiguities, schema_plan
                )
                field_ambiguities = _retain_true_business_ambiguities(
                    deps, scope, field_ambiguities
                )
                for ambiguous_slot in field_ambiguities:
                    marker = f"字段口径:{ambiguous_slot}"
                    if marker not in schema_plan.unresolved_slots:
                        schema_plan.unresolved_slots.append(marker)
                if field_ambiguities:
                    schema_plan.confidence *= 0.75
                projection_decision = resolve_vague_projection(
                    state, deps, query_intent, schema_plan, visible_tables, field_candidates
                )
                effective_graph, query_intent, schema_plan, projection_bindings = (
                    materialize_projection_decision(
                        projection_decision,
                        state.semantic_graph,
                        query_intent,
                        schema_plan,
                    )
                )
                semantic_coverage = refresh_semantic_coverage(
                    original_query, effective_graph, state.semantic_coverage
                )
                output_bindings = ground_output_bindings(
                    effective_graph,
                    field_candidates,
                    state.selected_field_overrides,
                    visible_tables,
                    set(plan_table_names(schema_plan)),
                )
                output_bindings.update(projection_bindings)
                schema_plan = extend_schema_plan_for_output_bindings(
                    schema_plan,
                    output_bindings,
                    visible_tables,
                    relations,
                    max_hops=max_hops,
                )
                planned_names = plan_table_names(schema_plan)
                available = _catalog_hits(deps, scope)
                planned_hits = [available[name] for name in planned_names if name in available]
                if planned_hits:
                    merged = planned_hits
                    confidence = schema_plan.confidence
                    candidates = []
                    business_clarification, option_bindings = _business_clarification(
                        deps, scope, field_ambiguities
                    )
                    semantic_bindings = _ground_semantic_atoms(
                        state,
                        field_candidates,
                        visible_tables,
                        deps,
                        allowed_tables=set(planned_names),
                    )
                    unsupported_outputs = [
                        *_unsupported_output_concepts(effective_graph, output_bindings),
                        *(
                            projection_decision.missing_concepts
                            if projection_decision is not None
                            and not projection_decision.selected_fields
                            else []
                        ),
                    ]
                    unsupported_outputs = list(dict.fromkeys(unsupported_outputs))
                    main_table_count = max(
                        1,
                        len(schema_plan.anchor_tables) + len(schema_plan.dimension_tables),
                    )
                    return {
                        "retrieved_schema": merged,
                        "retrieval_confidence": confidence,
                        "retrieval_candidates": candidates,
                        "main_table_count": main_table_count,
                        "query_intent": query_intent,
                        "semantic_graph": effective_graph,
                        "semantic_coverage": semantic_coverage,
                        "projection_decision": projection_decision,
                        "field_candidates": field_candidates[:30],
                        "field_ambiguities": field_ambiguities,
                        "schema_plan": schema_plan,
                        "business_clarification": business_clarification,
                        "business_option_bindings": option_bindings,
                        "semantic_bindings": semantic_bindings,
                        "output_bindings": output_bindings,
                        "unsupported_outputs": unsupported_outputs,
                        **({"final_answer": _unsupported_output_answer(unsupported_outputs)}
                           if unsupported_outputs else {}),
                        "decision_summary": _enrich_decision_summary(
                            state, deps, schema_plan, confidence, projection_decision
                        ),
                    }

            # 无术语命中:向量检索即主结果,全部保留(置信度低会走低置信澄清)
            main_hits = [h for h, _ in scored]
            relation_seeds: set[str] = set()
            if scored:
                top_score = scored[0][1]
                relation_seeds = {
                    hit.table_name
                    for hit, score in scored
                    if _candidate_is_close(deps, top_score, score)
                }
            relation_supplement = _relation_supplements(
                deps, relation_results, relation_seeds, scope
            )
            path_supplement = _join_path_supplements(deps, relation_seeds, scope)
            merged = _dedupe(main_hits + relation_supplement + path_supplement)

        if use_term_hits:
            # 术语精确命中:置信度高;多张候选表(主表)时交给候选澄清
            uniq = _dedupe(hits)
            confidence = 1.0
            candidates = uniq if len(uniq) > 1 and not has_required_multi_table_hit else []
            main_table_count = len(uniq)  # 术语命中主表数(供复杂度判断,不含补充关联表)
        else:
            main_table_count = 0
            if scored:
                top_conf = scored[0][1]
                candidates = [h for h, c in scored if _candidate_is_close(deps, top_conf, c)]
                confidence = top_conf
            else:
                confidence = 0.0
                candidates = []

        business_clarification, option_bindings = _business_clarification(
            deps, scope, field_ambiguities
        )
        output_bindings = ground_output_bindings(
            state.semantic_graph,
            field_candidates,
            state.selected_field_overrides,
        )
        unsupported_outputs = (
            _unsupported_output_concepts(state.semantic_graph, output_bindings)
            if field_candidates else []
        )
        return {
            "retrieved_schema": merged,
            "retrieval_confidence": confidence,
            "retrieval_candidates": _dedupe(candidates),
            "main_table_count": main_table_count,
            "query_intent": query_intent,
            "field_candidates": field_candidates[:30],
            "field_ambiguities": field_ambiguities,
            "schema_plan": schema_plan,
            "business_clarification": business_clarification,
            "business_option_bindings": option_bindings,
            "semantic_bindings": _ground_semantic_atoms(
                state, field_candidates, visible_tables, deps
            ),
            "output_bindings": output_bindings,
            "unsupported_outputs": unsupported_outputs,
            **({"final_answer": _unsupported_output_answer(unsupported_outputs)}
               if unsupported_outputs else {}),
            "decision_summary": _enrich_decision_summary(
                state, deps, schema_plan, confidence
            ),
        }

    return schema_retrieval_node
