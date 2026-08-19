"""业务语义图的确定性降级解析与旧 QueryIntent 适配。

LLM 负责开放词汇理解；这里保证常见比较、状态、存在和布尔结构在模型不可用时
仍不会静默丢失。语义图是唯一解析事实，适配器只为迁移期旧 SchemaPlanner 服务。
"""

from __future__ import annotations

import re
from typing import Iterable

from nl2sql_agent.services.schema_planner import parse_query_intent
from nl2sql_agent.services.semantic_query import enrich_semantic_graph, metric_semantics
from nl2sql_agent.state import (
    IntentSlot,
    QueryAssumption,
    QueryIntent,
    SemanticGraph,
    SemanticOutput,
    SemanticPredicate,
    SemanticSubject,
)


_STATUS_PATTERN = re.compile(
    r"(?P<polarity>没有|不存在|无|未发生|有|存在|出现|发生过|发生)"
    r"(?P<concept>逾期|代偿|核销|还款)"
)
_AGGREGATE_COMPARISON_RE = re.compile(
    r"(?P<prefix>累计|合计|总计|平均|最大|最高|最小|最低)"
    r"(?P<field>[\u4e00-\u9fffA-Za-z_]{1,20}?)"
    r"(?P<word>不低于|至少|超过|大于|高于|不超过|至多|低于|小于|少于|等于)"
    r"\s*(?P<value>-?\d+(?:\.\d+)?)"
)
_COMPARISON_OPERATORS = {
    "不低于": ">=", "至少": ">=", "超过": ">", "大于": ">", "高于": ">",
    "不超过": "<=", "至多": "<=", "低于": "<", "小于": "<", "少于": "<", "等于": "=",
}


def _predicate_rules(config: dict | None) -> dict[str, dict]:
    return (config or {}).get("business_predicates", config or {})


def _matching_rule(concept: str, config: dict | None) -> dict:
    for rule in _predicate_rules(config).values():
        aliases = [str(item) for item in rule.get("aliases", [])]
        if concept == rule.get("concept") or concept in aliases:
            return rule
    return {}


def build_semantic_graph(query: str, config: dict | None = None) -> SemanticGraph:
    legacy = parse_query_intent(query)
    subjects: list[SemanticSubject] = []
    subject_ids: dict[str, str] = {}

    def add_subject(concept: str, kind: str) -> str:
        if concept not in subject_ids:
            subject_id = f"subject_{len(subject_ids) + 1}"
            subject_ids[concept] = subject_id
            subjects.append(SemanticSubject(id=subject_id, kind=kind, concept=concept))
        return subject_ids[concept]

    for item in legacy.entities:
        add_subject(item.text, "entity")
    if "贷款" in query:
        add_subject("贷款", "event")
    if not subjects:
        add_subject("查询对象", "concept")

    outputs = [
        SemanticOutput(
            id=f"output_{index}",
            subject_id=subject_ids.get("客户", subjects[0].id),
            concept=item.text,
            grounding_concept=item.text,
            source_text=item.text,
            source_span=(
                [query.rfind(item.text), query.rfind(item.text) + len(item.text)]
                if item.text and query.rfind(item.text) >= 0 else []
            ),
            required=True,
            confidence=0.9,
        )
        for index, item in enumerate(legacy.attributes, start=1)
        if item.text not in {"基本信息", "详细信息", "联系方式", "明细"}
    ]

    atoms: list[SemanticPredicate] = []
    atom_index = 1
    for item in legacy.filters:
        if isinstance(item.value, (int, float)):
            operator_words = "超过|大于|高于|不低于|至少|不超过|至多|低于|少于|等于"
        else:
            operator_words = "等于|为|是"
        text_pattern = re.compile(
            rf"{re.escape(item.text)}.*?(?:{operator_words})"
            rf"\s*{re.escape(str(item.value))}"
        )
        match = text_pattern.search(query)
        aggregate_prefix = next((
            prefix for prefix in ("累计", "合计", "总计", "平均", "最大", "最小")
            if f"{prefix}{item.text}" in query
        ), None)
        aggregate_label = f"{aggregate_prefix or ''}{item.text}"
        base_concept, aggregation, _ = metric_semantics(aggregate_label)
        atoms.append(SemanticPredicate(
            atom_id=f"atom_{atom_index}",
            predicate_type="aggregate_comparison" if aggregation else "comparison",
            subject_id=subject_ids.get("贷款", subjects[0].id),
            concept=item.text,
            grounding_concept=base_concept if aggregation else None,
            operator=item.operator,
            value=item.value,
            scope=(
                "per_entity" if aggregation and legacy.dimensions
                else "global" if aggregation
                else "same_record" if "贷款" in query
                else "record"
            ),
            source_text=match.group(0) if match else item.text,
            source_span=list(match.span()) if match else [],
            confidence=0.96,
        ))
        atom_index += 1

    # Repair aggregate-result comparisons independently from the legacy slot
    # parser, which intentionally strips words such as “累计/平均” when ranking
    # physical fields.  Those modifiers determine WHERE versus HAVING and must
    # remain in the semantic contract.
    for match in _AGGREGATE_COMPARISON_RE.finditer(query):
        raw_value = match.group("value")
        value = float(raw_value) if "." in raw_value else int(raw_value)
        label = f"{match.group('prefix')}{match.group('field')}"
        base, aggregation, _ = metric_semantics(label)
        replacement = SemanticPredicate(
            atom_id="",
            predicate_type="aggregate_comparison",
            subject_id=subject_ids.get("贷款", subjects[0].id),
            concept=label,
            grounding_concept=base,
            operator=_COMPARISON_OPERATORS[match.group("word")],
            value=value,
            scope="per_entity" if legacy.dimensions else "global",
            source_text=match.group(0),
            source_span=list(match.span()),
            confidence=0.98,
        )
        existing_index = next((
            index for index, atom in enumerate(atoms)
            if atom.value == value
            and atom.operator == replacement.operator
            and atom.predicate_type == "comparison"
            and (
                not atom.source_span
                or not (
                    atom.source_span[1] <= match.start()
                    or atom.source_span[0] >= match.end()
                )
            )
        ), None)
        if existing_index is not None:
            replacement.atom_id = atoms[existing_index].atom_id
            atoms[existing_index] = replacement
        else:
            replacement.atom_id = f"atom_{atom_index}"
            atoms.append(replacement)
            atom_index += 1

    assumptions: list[QueryAssumption] = []
    unresolved: list[str] = []
    for match in _STATUS_PATTERN.finditer(query):
        concept = match.group("concept")
        negative = match.group("polarity") in {"没有", "不存在", "无", "未发生"}
        rule = _matching_rule(concept, config)
        temporal = str(rule.get("temporal_default") or "unresolved")
        if temporal == "unresolved":
            unresolved.append(f"“{concept}”是当前状态还是历史发生过")
        elif rule:
            assumptions.append(QueryAssumption(
                content=str(rule.get("assumption") or f"“{concept}”按{temporal}状态理解"),
                source="configured_default",
                materiality="high",
            ))
        status_atom = SemanticPredicate(
            atom_id=f"atom_{atom_index}_status",
            predicate_type="status",
            subject_id=subject_ids.get(str(rule.get("subject") or "贷款"), subject_ids.get("贷款", subjects[0].id)),
            concept=concept,
            grounding_concept=str(rule.get("binding_concept") or concept),
            operator=str(rule.get("operator") or "=") if rule else None,
            value=rule.get("value") if rule else None,
            scope="same_record" if "其他" not in query else "related_set",
            temporal_mode=temporal if temporal in {"current", "historical", "range", "unresolved"} else "unresolved",
            source_text=match.group(0),
            source_span=list(match.span()),
            confidence=0.94 if rule else 0.75,
        )
        atoms.append(SemanticPredicate(
            atom_id=f"atom_{atom_index}",
            predicate_type="not_exists" if negative else "exists",
            subject_id=subject_ids.get("客户", subjects[0].id),
            concept=str(rule.get("related_concept") or "贷款"),
            scope="related_set",
            children=[status_atom],
            source_text=match.group(0),
            source_span=list(match.span()),
            confidence=status_atom.confidence,
        ))
        atom_index += 1

    # 对“金额条件且有状态的实体”显式确定作用域。经治理的 same_record 默认表示
    # 两个条件必须落在同一条关联事实记录中；没有默认规则时必须暴露为歧义。
    defaults = (config or {}).get("semantic_defaults", {})
    same_record_pair = (
        len(atoms) == 2
        and atoms[0].predicate_type == "comparison"
        and atoms[1].predicate_type == "exists"
        and atoms[1].children
        and atoms[1].children[0].predicate_type == "status"
        and "其他" not in query
        and "或" not in query
    )
    if same_record_pair and defaults.get("conjunction_scope") == "same_record":
        exists_atom = atoms[1]
        atoms = [exists_atom.model_copy(update={
            "children": [atoms[0], *exists_atom.children],
            "source_text": query,
        })]
        assumptions.append(QueryAssumption(
            content=str(defaults.get("conjunction_scope_assumption") or "并列条件按同一业务记录理解"),
            source="configured_default",
            materiality="high",
        ))
    elif same_record_pair:
        unresolved.append("并列条件是否必须发生在同一笔业务记录")

    if len(atoms) > 1:
        predicate_type = "or" if "或" in query and "且" not in query and "并且" not in query else "and"
        predicate = SemanticPredicate(
            atom_id="boolean_root",
            predicate_type=predicate_type,
            children=atoms,
            source_text=query,
            confidence=min(item.confidence for item in atoms),
        )
    else:
        predicate = atoms[0] if atoms else None

    capabilities = []
    for capability, enabled in (
        ("entity_output", bool(outputs or legacy.entities)),
        ("comparison", bool(legacy.filters)),
        ("existence", any(item.predicate_type in {"exists", "not_exists"} for item in atoms)),
        ("status", any(atom.predicate_type == "status" for atom in iter_semantic_atoms(predicate))),
        ("aggregation", legacy.query_type in {"aggregation", "multi_fact", "composite_metric"}),
    ):
        if enabled:
            capabilities.append(capability)
    return enrich_semantic_graph(query, SemanticGraph(
        subjects=subjects,
        outputs=outputs,
        predicate=predicate,
        capabilities=capabilities,
        assumptions=assumptions,
        unresolved_slots=list(dict.fromkeys(unresolved)),
        confidence=min((item.confidence for item in atoms), default=0.72),
    ))


def iter_semantic_atoms(predicate: SemanticPredicate | None) -> Iterable[SemanticPredicate]:
    if predicate is None:
        return
    if predicate.predicate_type not in {"and", "or", "not"}:
        yield predicate
    for child in predicate.children:
        yield from iter_semantic_atoms(child)


def required_atom_ids(graph: SemanticGraph | None) -> set[str]:
    if graph is None:
        return set()
    return {
        atom.atom_id
        for atom in iter_semantic_atoms(graph.predicate)
        if atom.materiality == "high"
    }


def semantic_atom_map(graph: SemanticGraph | None) -> dict[str, SemanticPredicate]:
    if graph is None:
        return {}
    return {atom.atom_id: atom for atom in iter_semantic_atoms(graph.predicate)}


def semantic_graph_to_query_intent(
    graph: SemanticGraph,
    query: str,
    config: dict | None = None,
) -> QueryIntent:
    """迁移适配：从唯一语义图派生旧 QueryIntent，绝不重新解析并覆盖语义。"""
    legacy = parse_query_intent(query)
    filters: list[IntentSlot] = []
    measures: list[IntentSlot] = []
    for atom in iter_semantic_atoms(graph.predicate):
        if atom.predicate_type in {"comparison", "aggregate_comparison"}:
            filters.append(IntentSlot(
                text=atom.grounding_concept or atom.concept,
                role="measure" if isinstance(atom.value, (int, float)) else "attribute",
                operator=atom.operator,
                value=atom.value,
            ))
            if isinstance(atom.value, (int, float)):
                measures.append(IntentSlot(
                    text=atom.grounding_concept or atom.concept, role="measure"
                ))
        elif atom.predicate_type == "status":
            rule = _matching_rule(atom.concept, config)
            binding_concept = str(rule.get("binding_concept") or atom.concept)
            filters.append(IntentSlot(
                text=binding_concept,
                role="status",
                operator=str(rule.get("operator") or "="),
                value=rule.get("value", True),
            ))
    aggregate_outputs = [
        output for output in graph.outputs
        if output.required and output.aggregation
    ]
    query_type = (
        "fact_filter" if filters
        else "multi_fact" if len(aggregate_outputs) > 1
        else "aggregation" if aggregate_outputs or graph.group_by
        else legacy.query_type
    )
    output_attributes = [
        IntentSlot(text=output.grounding_concept or output.concept, role="attribute")
        for output in graph.outputs
        if output.required
        and (not output.aggregation or output.broad)
        and (output.grounding_concept or output.concept)
    ]
    output_measures = [
        IntentSlot(text=output.grounding_concept or output.concept, role="measure")
        for output in aggregate_outputs
        if output.aggregation != "count_distinct" and (output.grounding_concept or output.concept)
    ]
    dimensions = [IntentSlot(text=item, role="dimension") for item in graph.group_by]
    entity_names = list(dict.fromkeys([
        *[item.text for item in legacy.entities],
        *graph.group_by,
    ]))
    return legacy.model_copy(update={
        "query_type": query_type,
        "entities": [IntentSlot(text=item, role="entity") for item in entity_names],
        "filters": filters,
        "measures": list({
            item.text: item for item in [*output_measures, *measures]
        }.values()),
        "attributes": list({
            item.text: item for item in output_attributes
        }.values()),
        "dimensions": dimensions or legacy.dimensions,
    })
