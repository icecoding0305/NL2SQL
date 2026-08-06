"""全局状态定义。

规格中的状态字段保持原样,另补充少量运行时字段:
- clarification_questions / sensitive_reasons / complex_reasons:节点产出,供人工确认与调试
- blocked_reason:危险 SQL 硬失败标记(不进入重试)
- trace_steps / node_latencies:链路追踪,配合 graph 里的 traced 包装器写入
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SchemaHit(BaseModel):
    """一次 Schema 检索命中:一张表及其列定义(含敏感标记/注释)。"""

    table_name: str
    columns: list[dict] = Field(default_factory=list)  # {name, type, comment, sensitive?}
    business_terms: list[str] = Field(default_factory=list)  # 命中的术语


class IntentSlot(BaseModel):
    """从用户问题中抽取的业务槽位；此阶段不绑定物理 Schema。"""

    text: str
    role: Literal["measure", "attribute", "entity", "dimension", "time", "status"]
    operator: Optional[str] = None
    value: Any = None


class QueryIntent(BaseModel):
    """查询结构，用于驱动字段召回和表角色规划。"""

    query_type: Literal[
        "attribute_lookup", "fact_filter", "aggregation", "event_detail",
        "multi_fact", "existence", "composite_metric", "unknown",
    ] = "unknown"
    entities: list[IntentSlot] = Field(default_factory=list)
    measures: list[IntentSlot] = Field(default_factory=list)
    attributes: list[IntentSlot] = Field(default_factory=list)
    filters: list[IntentSlot] = Field(default_factory=list)
    dimensions: list[IntentSlot] = Field(default_factory=list)


class QueryAssumption(BaseModel):
    """系统在理解问题时采用的可审计假设，而不是内部思维过程。"""

    content: str
    source: Literal["user", "conversation", "configured_default", "system_inference"]
    materiality: Literal["low", "medium", "high"] = "low"


class SemanticSubject(BaseModel):
    id: str
    kind: Literal["entity", "event", "state", "concept"]
    concept: str


class SemanticOutput(BaseModel):
    id: str
    subject_id: str
    concept: str
    source_text: str = ""


class SemanticPredicate(BaseModel):
    """与物理 Schema 无关的类型化业务谓词。"""

    atom_id: str
    predicate_type: Literal[
        "comparison", "status", "exists", "not_exists", "temporal",
        "membership", "text_match", "aggregate_comparison", "and", "or", "not",
    ]
    subject_id: Optional[str] = None
    concept: str = ""
    grounding_concept: Optional[str] = None
    operator: Optional[str] = None
    value: Any = None
    scope: Literal[
        "record", "same_record", "related_set", "per_entity", "global", "unresolved",
    ] = "record"
    temporal_mode: Literal["current", "historical", "range", "unresolved", "none"] = "none"
    children: list["SemanticPredicate"] = Field(default_factory=list)
    source_text: str = ""
    source_span: list[int] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    materiality: Literal["low", "medium", "high"] = "high"


class SemanticGraph(BaseModel):
    """自然语言问题的唯一业务语义事实；后续节点不得重新解析覆盖。"""

    subjects: list[SemanticSubject] = Field(default_factory=list)
    outputs: list[SemanticOutput] = Field(default_factory=list)
    predicate: Optional[SemanticPredicate] = None
    capabilities: list[str] = Field(default_factory=list)
    assumptions: list[QueryAssumption] = Field(default_factory=list)
    unresolved_slots: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ResolvedQuery(BaseModel):
    """进入 Schema 检索前的规范化业务问题。"""

    original_query: str
    rewritten_query: str
    query_type: Literal[
        "attribute_lookup", "fact_filter", "aggregation", "event_detail",
        "multi_fact", "existence", "composite_metric", "unknown",
    ] = "unknown"
    entities: list[str] = Field(default_factory=list)
    measures: list[str] = Field(default_factory=list)
    attributes: list[str] = Field(default_factory=list)
    filters: list[dict] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    assumptions: list[QueryAssumption] = Field(default_factory=list)
    unresolved_business_slots: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    semantic_graph: Optional[SemanticGraph] = None


class BusinessClarificationOption(BaseModel):
    """面向业务用户的选项；不得包含物理表名或字段名。"""

    id: str
    label: str
    description: str = ""


class BusinessClarification(BaseModel):
    slot: str
    question: str
    options: list[BusinessClarificationOption] = Field(default_factory=list)


class DecisionSource(BaseModel):
    business_name: str
    role: str
    reason: str = ""


class DecisionSummary(BaseModel):
    """可向用户展示的决策摘要，不包含模型内部思维链。"""

    understood_query: str
    business_steps: list[str] = Field(default_factory=list)
    data_sources: list[DecisionSource] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    confidence: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class FieldCandidate(BaseModel):
    """一个查询槽位对应的字段候选及可解释评分。"""

    table_name: str
    column_name: str
    query_slot: str
    semantic_role: str = ""
    data_type: str = ""
    vector_score: float = 0.0
    lexical_score: float = 0.0
    phrase_coverage: float = 0.0
    type_role_score: float = 0.0
    final_score: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class PlannedTable(BaseModel):
    table_name: str
    role: Literal["primary_fact", "secondary_fact", "entity", "dimension", "bridge"]
    selected_columns: list[str] = Field(default_factory=list)
    reason: str = ""
    score: float = 0.0


class SchemaPlan(BaseModel):
    """字段驱动的最小连通 Schema 子图。"""

    anchor_tables: list[PlannedTable] = Field(default_factory=list)
    dimension_tables: list[PlannedTable] = Field(default_factory=list)
    bridge_tables: list[PlannedTable] = Field(default_factory=list)
    relations: list[dict] = Field(default_factory=list)
    unresolved_slots: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class _PlanPart(BaseModel):
    """查询计划子结构的严格基类，并保留旧代码的只读下标访问方式。"""

    model_config = ConfigDict(extra="forbid")

    def __getitem__(self, key: str):
        return getattr(self, key)


class JoinSpec(_PlanPart):
    left_table: str
    right_table: str
    left_column: str
    right_column: str
    join_type: Literal["inner", "left", "right", "full", "cross"] = "inner"
    source_atom_ids: list[str] = Field(default_factory=list)

    @field_validator("join_type", mode="before")
    @classmethod
    def normalize_join_type(cls, value):
        return str(value or "inner").strip().lower().replace(" outer", "")


class FilterSpec(_PlanPart):
    column: str
    operator: Literal[
        "=", "!=", "<>", ">", ">=", "<", "<=",
        "in", "not in", "between", "like", "not like", "is", "is not",
    ]
    value: Any = None
    table: Optional[str] = None
    source_atom_ids: list[str] = Field(default_factory=list)

    @field_validator("operator", mode="before")
    @classmethod
    def normalize_operator(cls, value):
        return " ".join(str(value).strip().lower().split())


class MetricSpec(_PlanPart):
    metric_name: str
    definition: str
    columns: list[str] = Field(default_factory=list)
    expression: Optional[str] = None
    source_atom_ids: list[str] = Field(default_factory=list)


class OutputFieldSpec(_PlanPart):
    """A field explicitly returned by the query."""

    concept: str = ""
    table: Optional[str] = None
    column: Optional[str] = None
    expression: Optional[str] = None
    alias: Optional[str] = None
    aggregation: Optional[Literal[
        "count", "count_distinct", "sum", "avg", "min", "max",
    ]] = None


class OutputGrain(_PlanPart):
    """The semantic grain of one output row."""

    level: Literal["entity", "record", "aggregate", "global", "unknown"] = "unknown"
    entity: Optional[str] = None
    keys: list[str] = Field(default_factory=list)
    description: str = ""


class QueryPlan(BaseModel):
    """查询计划。

    类型层面不表达任何写操作/DDL:没有 operation 字段,只有 SELECT 类元素
    (目标表、连接、过滤、指标口径、分组、置信度),危险操作在结构上不可表达。
    """

    model_config = ConfigDict(extra="forbid")

    target_tables: list[str] = Field(min_length=1)
    join_logic: list[JoinSpec] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    metric_logic: Optional[MetricSpec] = None
    group_by: list[str] = Field(default_factory=list)
    output_fields: list[OutputFieldSpec] = Field(default_factory=list)
    output_grain: OutputGrain = Field(default_factory=OutputGrain)
    covered_atom_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class QuerySchemaColumn(_PlanPart):
    name: str
    type: str = ""
    comment: str = ""
    semantic_role: str = ""
    primary_key: bool = False
    unique: bool = False
    nullable: bool = True


class QuerySchemaTable(_PlanPart):
    name: str
    comment: str = ""
    role: Literal["primary_fact", "secondary_fact", "entity", "dimension", "bridge", "unknown"] = "unknown"
    columns: list[QuerySchemaColumn] = Field(default_factory=list)
    primary_keys: list[str] = Field(default_factory=list)


class QuerySchemaRelation(_PlanPart):
    source_table: str
    source_columns: list[str] = Field(default_factory=list)
    target_table: str
    target_columns: list[str] = Field(default_factory=list)
    cardinality: Optional[str] = None
    status: str = ""


class QueryMSchema(BaseModel):
    """Minimal, query-scoped projection of the effective M-Schema."""

    model_config = ConfigDict(extra="forbid")
    tables: list[QuerySchemaTable] = Field(default_factory=list)
    relations: list[QuerySchemaRelation] = Field(default_factory=list)
    semantic_bindings: dict[str, dict] = Field(default_factory=dict)


class LogicalOperation(_PlanPart):
    id: str
    kind: Literal[
        "scan", "filter", "join", "semi_join", "anti_join",
        "aggregate", "project", "sort", "limit",
    ]
    inputs: list[str] = Field(default_factory=list)
    table: Optional[str] = None
    join: Optional[JoinSpec] = None
    predicates: list[FilterSpec] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    metric: Optional[MetricSpec] = None
    fields: list[OutputFieldSpec] = Field(default_factory=list)
    sort_by: list[str] = Field(default_factory=list)
    limit: Optional[int] = Field(default=None, ge=1)
    source_atom_ids: list[str] = Field(default_factory=list)


class LogicalPlan(BaseModel):
    """Relational-algebra plan used as the stable boundary before SQL compilation."""

    model_config = ConfigDict(extra="forbid")
    operations: list[LogicalOperation] = Field(min_length=1)
    root_operation_id: str
    output_fields: list[OutputFieldSpec] = Field(default_factory=list)
    output_grain: OutputGrain = Field(default_factory=OutputGrain)
    covered_atom_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class NL2SQLState(BaseModel):
    # ---------- 模块 1:用户提问 ----------
    user_query: str
    conversation_history: list[dict] = Field(default_factory=list)
    clarified_query: Optional[str] = None
    need_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list)
    # 澄清原因:missing_time_range / ambiguous_candidates / low_confidence
    clarification_reason: Optional[str] = None

    # ---------- 模块 2:问题理解、改写与业务消歧 ----------
    resolved_query: Optional[ResolvedQuery] = None
    semantic_graph: Optional[SemanticGraph] = None
    # atom_id -> 已选物理字段及业务谓词约束，仅由 Schema Grounding 产生。
    semantic_bindings: dict[str, dict] = Field(default_factory=dict)
    business_clarification: Optional[BusinessClarification] = None
    # 选项到物理字段的内部绑定；API 不得下发给普通用户。
    business_option_bindings: dict[str, str] = Field(default_factory=dict)
    decision_summary: Optional[DecisionSummary] = None

    # ---------- 模块 2.5:查询理解 ----------
    query_intent: Optional[QueryIntent] = None

    # ---------- 模块 3:Schema 检索 ----------
    retrieved_schema: list[SchemaHit] = Field(default_factory=list)
    # 检索置信度与候选(模块 3.5 判定用)
    retrieval_confidence: float = 0.0
    retrieval_candidates: list[SchemaHit] = Field(default_factory=list)
    # 术语精确命中的主表数(不含向量补充的关联表),供复杂度判断
    main_table_count: int = 0
    low_confidence_flag: bool = False
    # 候选澄清已解决(用户选定后不再重复触发),由模块 3.5 内部维护
    retrieval_resolved: bool = False
    field_candidates: list[FieldCandidate] = Field(default_factory=list)
    field_ambiguities: dict[str, list[FieldCandidate]] = Field(default_factory=dict)
    schema_plan: Optional[SchemaPlan] = None
    # 用户对某个业务槽位选择的物理字段，例如 {"代偿金额": "claim.DC_ALL_BAL"}
    selected_field_overrides: dict[str, str] = Field(default_factory=dict)

    # ---------- 模块 4:复杂度判断 ----------
    is_complex: bool = False
    complex_reasons: list[str] = Field(default_factory=list)

    # ---------- 模块 5b/6:计划 ----------
    query_plan: Optional[QueryPlan] = None
    query_mschema: Optional[QueryMSchema] = None
    logical_plan: Optional[LogicalPlan] = None
    plan_normalizations: list[str] = Field(default_factory=list)
    plan_validation_errors: list[str] = Field(default_factory=list)
    plan_retry_count: int = 0
    max_plan_retries: int = 2

    # ---------- 模块 7/8:SQL 生成与校验 ----------
    generated_sql: Optional[str] = None
    used_tables: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 3
    # 危险操作(非 SELECT)硬失败,不进入重试
    blocked_reason: Optional[str] = None

    # ---------- 模块 9:敏感判定 / 人工确认 ----------
    is_sensitive: bool = False
    sensitive_reasons: list[str] = Field(default_factory=list)
    # pass / approval_required / hard_block；旧字段保留供前端兼容
    risk_decision: Literal["pass", "approval_required", "hard_block"] = "pass"
    human_approved: Optional[bool] = None

    # ---------- 模块 10/11:执行与结果解释 ----------
    execution_result: Optional[list[dict]] = None
    execution_error: Optional[str] = None
    final_answer: Optional[str] = None

    # ---------- 身份与权限(入口注入,下游只读) ----------
    user_id: str
    data_scope: list[str] = Field(default_factory=list)  # 用户可访问的业务线
    # 独立的可信行级权限，由服务端鉴权层注入；不得从 data_scope 推导，也不由查询 API 接收
    # 示例:{"PLATFORM_CODE": ["XXD", "ZJ"]}
    row_level_filters: dict[str, list[str]] = Field(default_factory=dict)

    # ---------- 追踪(预留:反馈闭环/语义缓存可在下游扩展) ----------
    trace_id: str = ""
    node_latencies: dict = Field(default_factory=dict)
    trace_steps: list[str] = Field(default_factory=list)
