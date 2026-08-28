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
  resolved_outputs: string[];
  excluded_outputs: string[];
  missing_outputs: string[];
}

export interface ResultSummary {
  status: "success" | "empty" | "partial";
  headline: string;
  overview: string;
  key_findings: string[];
  caveats: string[];
  row_count: number;
  summarized_row_count: number;
  truncated: boolean;
}

export interface QueryRecord {
  trace_id: string;
  conversation_id?: string;
  database_id?: string;
  user_id: string;
  user_query: string;
  data_scope: string[];
  status: "running" | "done" | "pending_review" | "error" | "rejected" | "blocked" | "cancelled";
  generated_sql?: string | null;
  plan_json?: Record<string, unknown> | null;
  retrieved_schema?: SchemaHit[];
  sensitive_reasons?: string[];
  execution_result?: Record<string, unknown>[] | null;
  execution_error?: string | null;
  result_summary?: ResultSummary | null;
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
  projection_decision?: Record<string, unknown> | null;
  clarification_reason?: string;
  created_at?: string;
  updated_at?: string;
  title?: string;
  turn_count?: number;
  finished_at?: string;
  feedbacks?: { id: number; node: string; feedback_type: string; comment: string }[];
}

export interface DatabaseConfig {
  id: string;
  name: string;
  engine: "mysql" | "postgres";
  host: string;
  port: number;
  database_name: string;
  username: string;
  namespace: string;
  is_default: boolean;
  password_configured: boolean;
  schema_status: "not_synced" | "syncing" | "ready" | "error";
  schema_message?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface SchemaOptionColumn {
  name: string;
  comment: string;
  type: string;
}

export interface SchemaOptionTable {
  table_name: string;
  comment: string;
  columns: SchemaOptionColumn[];
}

export interface DatabaseRelation {
  id: string;
  database_id: string;
  source_table: string;
  source_columns: string[];
  target_table: string;
  target_columns: string[];
  cardinality: "one_to_one" | "one_to_many" | "many_to_one" | "many_to_many" | "unknown";
  preferred_join_type: "inner" | "left";
  description?: string;
  enabled: boolean;
  status: "candidate" | "inferred" | "verified" | "confirmed" | "rejected";
  source: "user_configured" | "schema_relation_discovery" | string;
  confidence: number;
  evidence: string[];
  validation_summary: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export type KnowledgeType = "term" | "synonym" | "business_rule" | "optimization_case";
export type KnowledgeStatus = "draft" | "published" | "disabled";

export interface KnowledgeItem {
  id: string;
  knowledge_type: KnowledgeType;
  name: string;
  description: string;
  database_id?: string | null;
  namespace: string;
  status: KnowledgeStatus;
  priority: number;
  version: number;
  payload: Record<string, unknown>;
  source: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  published_at?: string | null;
}

export interface KnowledgeSummary {
  total: number;
  by_type: Record<KnowledgeType, number>;
  by_status: Record<KnowledgeStatus, number>;
}

export interface SchemaEvaluationMetrics {
  case_count: number;
  table_recall_at_k: number;
  column_recall: number;
  forbidden_table_rate: number;
  join_path_accuracy: number;
  schema_plan_exact_match: number;
  clarification_accuracy: number;
}

export interface SchemaEvaluationCase {
  id: string;
  suite: string;
  tags: string[];
  question: string;
  passed: boolean;
  predicted_tables: string[];
  expected_tables: string[];
  predicted_columns: string[];
  expected_columns: string[];
  predicted_joins: string[][];
  expected_joins: string[][];
  planned_tables: string[];
  schema_plan_exact: boolean | null;
  clarified: boolean;
  expected_clarification?: boolean;
  retrieval_confidence: number;
  unresolved_slots: string[];
  retrieval_evidence: Record<string, unknown>[];
}

export interface SchemaEvaluationReport {
  dataset_version: number;
  description: string;
  coverage: Record<string, unknown>;
  metrics: SchemaEvaluationMetrics;
  metrics_by_suite: Record<string, SchemaEvaluationMetrics>;
  cases: SchemaEvaluationCase[];
  duration_seconds: number;
  started_at: number;
  finished_at: number;
}

export interface SchemaEvaluationStatus {
  running: boolean;
  dataset: {
    version: number;
    description: string;
    coverage: Record<string, unknown>;
    case_count: number;
  };
  report: SchemaEvaluationReport | null;
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
    | "cancelled"
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
