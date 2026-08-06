// 前后端共享的类型定义(与后端 api.py / state.py 对齐)

export interface SchemaHit {
  table_name: string;
  columns: { name: string; type: string; comment?: string }[];
  business_terms: string[];
}

export interface FieldCandidate {
  table_name: string;
  column_name: string;
  query_slot: string;
  final_score: number;
  evidence: string[];
}

export interface SchemaPlanTable {
  table_name: string;
  role: "primary_fact" | "secondary_fact" | "entity" | "dimension" | "bridge";
  selected_columns: string[];
  reason: string;
  score: number;
}

export interface SchemaPlan {
  anchor_tables: SchemaPlanTable[];
  dimension_tables: SchemaPlanTable[];
  bridge_tables: SchemaPlanTable[];
  unresolved_slots: string[];
  confidence: number;
}

export interface BusinessClarification {
  slot: string;
  question: string;
  options: { id: string; label: string; description?: string }[];
}

export interface DecisionSummary {
  understood_query: string;
  business_steps: string[];
  data_sources: { business_name: string; role: string; reason?: string }[];
  assumptions: string[];
  confidence: Record<string, number>;
  warnings: string[];
}

export interface QueryRecord {
  trace_id: string;
  user_id: string;
  user_query: string;
  data_scope: string[];
  status: "running" | "done" | "pending_review" | "error" | "rejected" | "blocked";
  generated_sql?: string | null;
  plan_json?: Record<string, unknown> | null;
  retrieved_schema?: SchemaHit[];
  sensitive_reasons?: string[];
  execution_result?: Record<string, unknown>[] | null;
  execution_error?: string | null;
  final_answer?: string | null;
  trace_steps?: string[];
  node_latencies?: Record<string, number>;
  retry_count?: number;
  plan_retry_count?: number;
  approved?: boolean | null;
  approver?: string | null;
  next_node?: string | null;  // 暂停在哪个节点(human_review / clarify_candidates / clarify_low_confidence)
  retrieval_confidence?: number;
  retrieval_candidates?: SchemaHit[];
  field_candidates?: FieldCandidate[];
  field_ambiguities?: Record<string, FieldCandidate[]>;
  schema_plan?: SchemaPlan | null;
  resolved_query?: Record<string, unknown> | null;
  semantic_graph?: Record<string, unknown> | null;
  business_clarification?: BusinessClarification | null;
  decision_summary?: DecisionSummary | null;
  clarification_reason?: string;
  created_at?: string;
  finished_at?: string;
  feedbacks?: { id: number; node: string; feedback_type: string; comment: string }[];
}

// WebSocket 推送的 pipeline 节点事件(node 与后端节点名一致)
export interface PipelineEvent {
  event:
    | "trace"
    | "node_start"
    | "node_complete"
    | "retry"
    | "interrupt"
    | "final"
    | "error"
    | "done"
    | "ping"
    | "restore";
  node?: string | null;
  trace_id: string;
  data?: unknown;
  message?: string;
}

// 各模块的展示顺序与标题
export const PIPELINE_NODES: { node: string; title: string }[] = [
  { node: "query_resolution", title: "问题理解与改写" },
  { node: "schema_retrieval", title: "Schema 检索" },
  { node: "clarify_business", title: "业务口径确认" },
  { node: "clarify_low_confidence", title: "低置信提示" },
  { node: "plan_generation", title: "查询计划" },
  { node: "plan_validation", title: "计划校验" },
  { node: "sql_generation", title: "SQL 生成" },
  { node: "static_validation", title: "静态校验" },
  { node: "sensitive_check", title: "敏感判定" },
  { node: "human_review", title: "人工确认" },
  { node: "sandbox_execution", title: "沙箱执行" },
  { node: "result_interpretation", title: "结果解释" },
];
