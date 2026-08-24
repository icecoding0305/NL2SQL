import { useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  Card,
  Input,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import {
  ClockCircleOutlined,
  DatabaseOutlined,
  HistoryOutlined,
  PlusOutlined,
  DownOutlined,
  ExclamationCircleOutlined,
  RightOutlined,
  SendOutlined,
  LoadingOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { apiDelete, apiGet, apiPost, submitQuery, type QueryController } from "../api";
import { message } from "antd";
import {
  AnswerCard,
  ResultTable,
  RetryTimeline,
  SchemaRetrievalCard,
  StepStatusTag,
} from "../components/StepCards";
import ConversationThread from "../components/ConversationThread";
import HistorySidebar from "../components/HistorySidebar";
import {
  loadActiveSessions,
  removeActiveSession,
  saveActiveSession,
} from "../components/activeSessions";
import PlanPreviewCard from "../components/PlanPreviewCard";
import { APPROVAL_ENABLED } from "../config/features";
import SqlPreviewCard from "../components/SqlPreviewCard";
import { PIPELINE_NODES, type DatabaseConfig, type PipelineEvent, type QueryRecord, type SchemaHit } from "../types";

const { Text } = Typography;

// 后端存储的是本地时间字符串(如 2026-08-06T15:43:06),前端新会话用 UTC ISO(带 Z)。
// 两种格式混在同一个字符串排序里会导致新会话被排到中间/底部,故统一转成毫秒时间戳比较。
function toTimeMs(value?: string): number {
  if (!value) return 0;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? 0 : ms;
}

export interface StepState {
  status: "idle" | "running" | "done" | "interrupt" | "error";
  data?: Record<string, any>;
  retries: { attempt: number; reason: string }[];
}

interface Session {
  trace_id: string;
  conversation_id: string;
  query: string;
  data_scope: string[];
  database_id?: string;
  status: "running" | "pending_review" | "done" | "error" | "rejected" | "blocked" | "cancelled";
  steps: Record<string, StepState>;
  trace_steps: string[];
  node_latencies: Record<string, number>;
  created_at: string;
}

// ---------- 从最终 state(record/final.data)恢复各步骤 ----------

export function stepsFromState(data: Record<string, any>): Record<string, StepState> {
  const steps: Record<string, StepState> = {};
  const mk = (d?: any) => ({ status: "done" as const, data: d, retries: [] });
  if (data.resolved_query || data.decision_summary)
    steps.query_resolution = mk({
      resolved_query: data.resolved_query,
      decision_summary: data.decision_summary,
    });
  if (data.retrieved_schema)
    steps.schema_retrieval = mk({ retrieved_schema: data.retrieved_schema });
  if (data.plan_json || data.plan) steps.plan_generation = mk({ query_plan: data.plan || data.plan_json });
  if (data.generated_sql) {
    steps.sql_generation = mk({ generated_sql: data.generated_sql });
    steps.static_validation = mk({ generated_sql: data.generated_sql });
  }
  if (data.is_sensitive !== undefined)
    steps.sensitive_check = mk({ is_sensitive: data.is_sensitive, sensitive_reasons: data.sensitive_reasons || [] });
  if (data.sensitive_reasons && data.sensitive_reasons.length && data.status === "pending_review")
    steps.human_review = { status: "interrupt", data: {}, retries: [] };
  if (data.execution_result !== undefined && data.execution_result !== null)
    steps.sandbox_execution = mk({ execution_result: data.execution_result });
  if (data.execution_error) steps.sandbox_execution = { status: "error", data: { error: data.execution_error }, retries: [] };
  if (data.final_answer || data.result_summary)
    steps.result_interpretation = mk({ final_answer: data.final_answer, result_summary: data.result_summary });
  return steps;
}

function sessionFromRecord(rec: QueryRecord): Session {
  const status: Session["status"] = [
    "running", "pending_review", "done", "error", "rejected", "blocked", "cancelled",
  ].includes(rec.status) ? rec.status as Session["status"] : "done";
  return {
    trace_id: rec.trace_id,
    conversation_id: rec.conversation_id || rec.trace_id,
    query: rec.user_query,
    data_scope: rec.data_scope || [],
    database_id: rec.database_id,
    status,
    steps: stepsFromState(rec as any),
    trace_steps: rec.trace_steps || [],
    node_latencies: rec.node_latencies || {},
    created_at: rec.created_at || "",
  };
}

// ---------- 单步卡片 ----------

export function StepCard({
  node,
  title,
  step,
  traceId,
  onResume,
}: {
  node: string;
  title: string;
  step: StepState;
  traceId: string;
  onResume?: (traceId: string, resume: Record<string, any>) => void;
}) {
  const [expanded, setExpanded] = useState(
    step.status !== "done" || [
      "query_resolution", "sandbox_execution", "result_interpretation", "human_review",
    ].includes(node),
  );
  const data = step.data || {};
  let content: React.ReactNode = null;

  switch (node) {
    case "query_resolution": {
      const summary = data.decision_summary || {};
      const understoodQuery = summary.understood_query || data.resolved_query?.rewritten_query;
      content = step.status === "running" ? (
        <Text type="secondary">正在理解并整理你的问题…</Text>
      ) : understoodQuery ? (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Text>{understoodQuery}</Text>
          {(summary.business_steps || []).map((item: string, index: number) => (
            <Text key={item} type="secondary">{index + 1}. {item}</Text>
          ))}
          {(summary.resolved_outputs || []).length > 0 && (
            <div>
              <Text type="secondary">本次将返回</Text>
              <div style={{ marginTop: 6 }}>
                <Space size={[6, 6]} wrap>
                  {(summary.resolved_outputs || []).map((item: string) => (
                    <Tag key={item} color="blue">{item}</Tag>
                  ))}
                </Space>
              </div>
            </div>
          )}
          {(summary.excluded_outputs || []).length > 0 && (
            <div>
              <Text type="secondary">未自动返回</Text>
              {(summary.excluded_outputs || []).map((item: string) => (
                <div key={item}><Text type="secondary">- {item}</Text></div>
              ))}
            </div>
          )}
          {(summary.missing_outputs || []).length > 0 && (
            <Text type="warning">
              当前数据中未能确认：{summary.missing_outputs.join("、")}
            </Text>
          )}
          {(summary.assumptions || []).length > 0 && (
            <Text type="warning">关键假设：{summary.assumptions.join("；")}</Text>
          )}
          {(summary.warnings || []).length > 0 && (
            <Text type="warning">{summary.warnings.join("；")}</Text>
          )}
        </Space>
      ) : (
        <Text type="secondary">尚未形成问题理解结果</Text>
      );
      break;
    }
    case "clarify_business": {
      const clarification = data.business_clarification;
      content =
        step.status === "interrupt" && clarification ? (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Tag color="warning">{clarification.question}</Tag>
            <Space wrap>
              {(clarification.options || []).map((option: any) => (
                <Button
                  key={option.id}
                  size="small"
                  type="primary"
                  ghost
                  onClick={() => onResume?.(traceId, { option_id: option.id })}
                >
                  {option.label}
                </Button>
              ))}
            </Space>
          </Space>
        ) : (
          <Text type="secondary">候选已确认</Text>
        );
      break;
    }
    case "clarify_low_confidence":
      content =
        step.status === "interrupt" ? (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Tag color="warning">该指标不在已知范围,检索置信度较低,是否继续尝试?</Tag>
            <Text type="secondary">
              置信度: {typeof data.retrieval_confidence === "number" ? data.retrieval_confidence.toFixed(2) : "-"}
            </Text>
            <Space>
              <Button size="small" type="primary" onClick={() => onResume?.(traceId, { continue: true })}>
                继续
              </Button>
              <Button size="small" danger onClick={() => onResume?.(traceId, { continue: false })}>
                不继续
              </Button>
            </Space>
          </Space>
        ) : (
          <Text type="secondary">已确认继续(强制走计划路径并人工确认)</Text>
        );
      break;
    case "schema_retrieval":
      content = <SchemaRetrievalCard hits={(data.retrieved_schema || []) as SchemaHit[]} schemaPlan={data.schema_plan} />;
      break;
    case "plan_generation":
      content = data.query_plan ? (
        <PlanPreviewCard plan={data.query_plan as Record<string, any>} traceId={traceId} />
      ) : (
        <Text type="secondary">未生成计划</Text>
      );
      break;
    case "plan_validation":
      content =
        (data.plan_validation_errors || []).length > 0 ? (
          <Text type="danger">校验未通过: {(data.plan_validation_errors || []).join("；")}</Text>
        ) : (
          <Text type="success">计划校验通过</Text>
        );
      break;
    case "sql_generation":
    case "static_validation":
      content = data.generated_sql ? (
        <SqlPreviewCard sql={data.generated_sql} traceId={traceId} node={node} />
      ) : data.error ? (
        <Text type="danger">{data.error}</Text>
      ) : null;
      break;
    case "sensitive_check":
      content = data.is_sensitive ? (
        <Space direction="vertical">
          <Tag color={APPROVAL_ENABLED ? "red" : "orange"}>
            {APPROVAL_ENABLED ? "命中敏感规则,需人工确认" : "命中敏感规则,审批已临时关闭"}
          </Tag>
          {(data.sensitive_reasons || []).map((r: string) => (
            <Text key={r} type="secondary">
              - {r}
            </Text>
          ))}
        </Space>
      ) : (
        <Text type="secondary">未命中敏感规则</Text>
      );
      break;
    case "human_review":
      content = !APPROVAL_ENABLED ? (
        <Text type="secondary">人工审批已临时关闭</Text>
      ) :
        step.status === "interrupt" ? (
          <Space direction="vertical">
            <Tag icon={<ClockCircleOutlined />} color="warning">
              已提交审批,等待确认
            </Tag>
            <Text type="secondary">请到「审批队列」处理,或等待审批结果自动恢复</Text>
          </Space>
        ) : (
          <Text type="secondary">未触发人工确认</Text>
        );
      break;
    case "sandbox_execution":
      content =
        data.execution_result != null ? (
          <ResultTable rows={data.execution_result as Record<string, any>[]} />
        ) : data.error ? (
          <Text type="danger">{data.error}</Text>
        ) : null;
      break;
    case "result_interpretation":
      content = data.final_answer || data.result_summary ? (
        <AnswerCard answer={data.final_answer} summary={data.result_summary} />
      ) : null;
      break;
    default:
      content = null;
  }

  return (
    <Card
      className="pipeline-card"
      size="small"
      title={
        <Space>
          <StepStatusTag status={step.status} />
          <Text strong>{title}</Text>
        </Space>
      }
      extra={
        <Button
          type="text"
          size="small"
          icon={expanded ? <DownOutlined /> : <RightOutlined />}
          onClick={() => setExpanded((value) => !value)}
          aria-label={expanded ? `收起${title}` : `展开${title}`}
        />
      }
      styles={{ body: { padding: 0 } }}
    >
      <div className={`step-card-body ${expanded ? "expanded" : "collapsed"}`}>
        <div className="step-card-body-inner">
          <div style={{ padding: "17px 18px" }}>
            {step.status === "running" && !content ? (
              <Text type="secondary">
                处理中 <LoadingOutlined />
              </Text>
            ) : (
              content
            )}
            {step.retries.length > 0 && <RetryTimeline retries={step.retries} />}
          </div>
        </div>
      </div>
    </Card>
  );
}

// ---------- 主查询页 ----------

export default function QueryPage() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeConversation, setActiveConversation] = useState<string | null>(null);
  const [history, setHistory] = useState<QueryRecord[]>([]);
  const [dataScope, setDataScope] = useState<string[]>(["risk_mart"]);
  const [databases, setDatabases] = useState<DatabaseConfig[]>([]);
  const [databaseId, setDatabaseId] = useState<string>();
  const [input, setInput] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const controllers = useRef<Record<string, QueryController>>({});
  const [stoppingTraceId, setStoppingTraceId] = useState<string>();
  const activeTurns = useMemo(
    () => sessions
      .filter((session) => session.conversation_id === activeConversation)
      .sort((left, right) => toTimeMs(left.created_at) - toTimeMs(right.created_at)),
    [sessions, activeConversation],
  );
  const active = activeTurns[activeTurns.length - 1];

  // 加载历史会话列表
  const refreshHistory = () => {
    apiGet<QueryRecord[]>("/api/conversations?user_id=u1&limit=50")
      .then(setHistory)
      .catch(() => {});
  };
  useEffect(() => {
    refreshHistory();
    apiGet<DatabaseConfig[]>("/api/databases").then((items) => {
      setDatabases(items);
      const preferred = items.find((item) => item.is_default) || items[0];
      if (preferred) {
        setDatabaseId((current) => current || preferred.id);
        setDataScope([preferred.namespace]);
      }
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 从后端恢复"进行中/待审批"的活动会话(页面切换/刷新后不丢进度)
  useEffect(() => {
    const active = loadActiveSessions();
    for (const rec of active) {
      if (rec.status === "running" || rec.status === "pending_review") {
        // 先展示当前进度,再轮询最新状态
        const session = sessionFromRecord(rec);
        setSessions((prev) =>
          prev.some((s) => s.trace_id === rec.trace_id) ? prev : [session, ...prev],
        );
        apiGet<QueryRecord>(`/api/query/${rec.trace_id}`)
          .then((full) => {
            setActiveConversation((cur) => cur ?? (full.conversation_id || rec.trace_id));
            const restored = sessionFromRecord(full);
            setSessions((prev) =>
              prev.map((s) => (s.trace_id === rec.trace_id ? restored : s)),
            );
            if (full.status === "running" || full.status === "pending_review") {
              saveActiveSession(full);
              pollUntilDone(full.trace_id);
            } else {
              removeActiveSession(full.trace_id);
            }
          })
          .catch(() => {
            // 后端记录已删除或不可恢复时，不再让本地快照永久显示为“处理中”。
            removeActiveSession(rec.trace_id);
            setSessions((previous) => previous.filter(
              (session) => session.trace_id !== rec.trace_id,
            ));
          });
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const updateSession = (traceId: string, fn: (s: Session) => Session) => {
    setSessions((prev) => prev.map((s) => (s.trace_id === traceId ? fn(s) : s)));
  };

  const handleEvent = (traceId: string, e: PipelineEvent) => {
    if (["final", "interrupt", "error"].includes(e.event)) {
      window.setTimeout(refreshHistory, 200);
    }
    updateSession(traceId, (s) => {
      if (s.status === "cancelled") return s;
      const steps = { ...s.steps };
      switch (e.event) {
        case "trace":
          return { ...s, trace_id: e.trace_id };
        case "node_start":
          if (e.node) steps[e.node] = { status: "running", data: steps[e.node]?.data, retries: steps[e.node]?.retries || [] };
          return { ...s, steps };
        case "node_complete":
          if (e.node) {
            steps[e.node] = { status: "done", data: (e.data as Record<string, any>) || {}, retries: steps[e.node]?.retries || [] };
            const data = (e.data || {}) as Record<string, any>;
            if (e.node === "schema_retrieval" && data.decision_summary) {
              const understanding = steps.query_resolution || { status: "done", data: {}, retries: [] };
              steps.query_resolution = {
                ...understanding,
                data: { ...(understanding.data || {}), decision_summary: data.decision_summary },
              };
            }
          }
          return { ...s, steps };
        case "retry": {
          if (e.node) {
            const st = steps[e.node] || { status: "running", data: {}, retries: [] };
            steps[e.node] = { ...st, status: "running", retries: [...st.retries, (e.data as any) || {}] };
          }
          return { ...s, steps };
        }
        case "interrupt": {
          // 实际暂停节点:human_review / clarify_business / clarify_low_confidence
          const node = e.node || "human_review";
          steps[node] = { status: "interrupt", data: (e.data as Record<string, any>) || {}, retries: [] };
          return { ...s, status: "pending_review", steps };
        }
        case "final": {
          const data = (e.data || {}) as Record<string, any>;
          const finalStatus: Session["status"] = data.blocked_reason || data.risk_decision === "hard_block"
            ? "blocked"
            : "done";
          return {
            ...s,
            status: finalStatus,
            steps: stepsFromState(data),
            trace_steps: data.trace_steps || s.trace_steps,
            node_latencies: data.node_latencies || s.node_latencies,
          };
        }
        case "error":
          return { ...s, status: "error" };
        case "cancelled":
          return { ...s, status: "cancelled" };
        default:
          return s;
      }
    });
  };

  const submit = (query: string) => {
    if (!query.trim() || active?.status === "running" || active?.status === "pending_review") return;
    const selectedDatabase = databases.find((item) => item.id === databaseId);
    if (!selectedDatabase) return void message.warning("请先选择数据库");
    if (selectedDatabase.schema_status !== "ready") {
      return void message.warning("所选数据库尚未完成 Schema 同步");
    }
    const traceId = `t${Date.now()}`;
    const conversationId = activeConversation || `c${Date.now()}`;
    const session: Session = {
      trace_id: traceId,
      conversation_id: conversationId,
      query,
      data_scope: dataScope,
      database_id: selectedDatabase.id,
      status: "running",
      steps: {},
      trace_steps: [],
      node_latencies: {},
      created_at: new Date().toISOString(),
    };
    setSessions((prev) => [...prev, session]);
    setActiveConversation(conversationId);
    refreshHistory();
    saveActiveSession({
      trace_id: traceId,
      conversation_id: conversationId,
      user_id: "u1",
      user_query: query,
      data_scope: dataScope,
      database_id: selectedDatabase.id,
      status: "running",
      created_at: session.created_at,
    } as QueryRecord);
    // 上下文从当前会话已经完成的轮次构建；trace_id 只标识本次执行。
    const conversationHistory = activeTurns.flatMap((turn) => {
      const answer = turn.steps.result_interpretation?.data?.final_answer;
      return [
        { role: "user" as const, content: turn.query },
        ...(typeof answer === "string" && answer
          ? [{ role: "assistant" as const, content: answer }]
          : []),
      ];
    }).slice(-6);
    const ctrl = submitQuery(
      {
        user_query: query,
        user_id: "u1",
        data_scope: dataScope,
        database_id: selectedDatabase.id,
        trace_id: traceId,
        conversation_id: conversationId,
        conversation_history: conversationHistory,
      },
      (e) => handleEvent(traceId, e),
      () => undefined,
    );
    controllers.current[traceId] = ctrl;
  };

  const openHistory = async (rec: QueryRecord) => {
    const conversationId = rec.conversation_id || rec.trace_id;
    // 一次加载整段会话，历史追问按原顺序恢复，不重新发起查询。
    const turns = await apiGet<QueryRecord[]>(`/api/conversation/${conversationId}`)
      .catch(() => [rec]);
    const restored = turns.map(sessionFromRecord);
    setSessions((prev) => {
      const traceIds = new Set(restored.map((item) => item.trace_id));
      return [...prev.filter((item) => !traceIds.has(item.trace_id)), ...restored];
    });
    setActiveConversation(conversationId);
    const latestTurn = turns[turns.length - 1];
    if (latestTurn?.data_scope?.length) setDataScope(latestTurn.data_scope);
    if (latestTurn?.database_id) setDatabaseId(latestTurn.database_id);
    for (const turn of turns) {
      if (turn.status === "running" || turn.status === "pending_review") {
        saveActiveSession(turn);
        pollUntilDone(turn.trace_id);
      } else {
        removeActiveSession(turn.trace_id);
      }
    }
  };

  const pollUntilDone = (traceId: string) => {
    const timer = setInterval(async () => {
      const rec = await apiGet<QueryRecord>(`/api/query/${traceId}`).catch(() => null);
      if (!rec) return clearInterval(timer);
      if (rec.status === "done" || rec.status === "error" || rec.status === "blocked" || rec.status === "rejected" || rec.status === "cancelled") {
        clearInterval(timer);
        removeActiveSession(traceId);
        updateSession(traceId, (s) => ({ ...s, status: rec.status, steps: stepsFromState(rec as any) }));
      } else if (rec.status === "pending_review" && rec.next_node) {
        // 新的中断(人工确认/候选澄清/低置信澄清):刷新中断卡片,等待用户操作
        clearInterval(timer);
        updateSession(traceId, (s) => {
          const steps = stepsFromState(rec as any);
          steps[rec.next_node!] = { status: "interrupt", data: rec as any, retries: [] };
          return {
            ...s,
            status: "pending_review",
            steps,
            trace_steps: rec.trace_steps || [],
            node_latencies: rec.node_latencies || {},
          };
        });
      } else if (rec.status === "running") {
        // 进行中:更新到最新步骤进度
        updateSession(traceId, (s) => ({
          ...s,
          status: "running",
          steps: stepsFromState(rec as any),
          trace_steps: rec.trace_steps || s.trace_steps,
          node_latencies: rec.node_latencies || s.node_latencies,
        }));
      }
    }, 1500);
  };

  const doResume = async (traceId: string, resume: Record<string, any>) => {
    updateSession(traceId, (s) => ({ ...s, status: "running" }));
    try {
      await apiPost(`/api/query/${traceId}/resume`, { resume });
    } catch {
      /* resume 后台执行,通过轮询拿结果 */
    }
    pollUntilDone(traceId);
  };

  const stopActiveQuery = async () => {
    if (!active || !["running", "pending_review"].includes(active.status)) return;
    const traceId = active.trace_id;
    setStoppingTraceId(traceId);
    try {
      await apiPost(`/api/query/${traceId}/cancel`);
      controllers.current[traceId]?.close();
      delete controllers.current[traceId];
      removeActiveSession(traceId);
      updateSession(traceId, (session) => ({ ...session, status: "cancelled" }));
      window.setTimeout(refreshHistory, 100);
      message.success("查询已停止");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "停止查询失败");
    } finally {
      setStoppingTraceId(undefined);
    }
  };

  const deleteConversation = async (record: QueryRecord) => {
    try {
      const conversationId = record.conversation_id || record.trace_id;
      await apiDelete(`/api/conversation/${conversationId}`);
      sessions
        .filter((session) => session.conversation_id === conversationId)
        .forEach((session) => {
          controllers.current[session.trace_id]?.close();
          delete controllers.current[session.trace_id];
          removeActiveSession(session.trace_id);
        });
      setSessions((previous) => previous.filter(
        (session) => session.conversation_id !== conversationId,
      ));
      setHistory((previous) => previous.filter(
        (item) => (item.conversation_id || item.trace_id) !== conversationId,
      ));
      if (activeConversation === conversationId) setActiveConversation(null);
      message.success("对话已删除");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除失败");
    }
  };

  // 对活跃的 pending_review 会话轮询审批结果
  useEffect(() => {
    if (!active || active.status !== "pending_review") return;
    pollUntilDone(active.trace_id);
  }, [active?.trace_id, active?.status]);

  const liveConversationRecords = Array.from(
    sessions.reduce((grouped, session) => {
      const existing = grouped.get(session.conversation_id);
      if (!existing) {
        grouped.set(session.conversation_id, {
          trace_id: session.trace_id,
          conversation_id: session.conversation_id,
          user_id: "u1",
          user_query: session.query,
          title: session.query,
          data_scope: session.data_scope,
          database_id: session.database_id,
          status: session.status,
          created_at: session.created_at,
          updated_at: session.created_at,
          turn_count: 1,
        });
      } else {
        existing.trace_id = session.trace_id;
        existing.status = session.status;
        existing.updated_at = session.created_at;
        existing.turn_count = (existing.turn_count || 1) + 1;
      }
      return grouped;
    }, new Map<string, QueryRecord>()).values(),
  );
  const sidebarRecords: QueryRecord[] = [
    ...liveConversationRecords,
    ...history.filter((record) => !liveConversationRecords.some(
      (live) => live.conversation_id === (record.conversation_id || record.trace_id),
    )),
  ].sort((left, right) => (
    toTimeMs(right.updated_at || right.created_at) - toTimeMs(left.updated_at || left.created_at)
  ));
  const isConversationBusy = active?.status === "running" || active?.status === "pending_review";

  return (
    <div className="query-workspace">
      <HistorySidebar
        open={historyOpen}
        records={sidebarRecords}
        activeConversation={activeConversation}
        onOpen={(record) => { openHistory(record); setHistoryOpen(false); }}
        onRefresh={refreshHistory}
        onDelete={deleteConversation}
        onNew={() => { setActiveConversation(null); setInput(""); setHistoryOpen(false); }}
        onClose={() => setHistoryOpen(false)}
      />

      <main className="conversation-main">
        <div className="conversation-toolbar">
          <div className="conversation-toolbar-main">
            <Button type="text" icon={<HistoryOutlined />} onClick={() => setHistoryOpen(true)}>历史会话</Button>
            <div className="conversation-title-block">
              <Text strong>{active ? "数据对话" : "新对话"}</Text>
              <Text type="secondary">{activeTurns.length ? `${activeTurns.length} 轮问答` : "开始一次新的数据探索"}</Text>
            </div>
          </div>
          <Space className="conversation-toolbar-actions">
            <DatabaseOutlined className="toolbar-database-icon" />
            <Select
              className="scope-select"
              value={databaseId}
              onChange={(value) => {
                setDatabaseId(value);
                const selected = databases.find((item) => item.id === value);
                if (selected) setDataScope([selected.namespace]);
              }}
              disabled={Boolean(activeConversation)}
              style={{ minWidth: 240 }}
              placeholder="请选择数据库"
              aria-label="查询数据库"
              options={databases.map((item) => ({
                value: item.id,
                label: `${item.name}${item.is_default ? "（默认）" : ""}`,
                disabled: item.schema_status !== "ready",
              }))}
            />
            <Button icon={<PlusOutlined />} onClick={() => { setActiveConversation(null); setInput(""); }}>新建</Button>
          </Space>
        </div>
        <div className="conversation-scroll">
          <ConversationThread
            sessions={activeTurns}
            databaseName={databases.find((item) => item.id === databaseId)?.name}
            onSuggestion={setInput}
            renderStep={(turn, node, title) => {
              const step = turn.steps[node];
              if (!step || step.status === "idle") return null;
              return (
                <StepCard
                  key={`${turn.trace_id}-${node}`}
                  node={node}
                  title={title}
                  step={step}
                  traceId={turn.trace_id}
                  onResume={doResume}
                />
              );
            }}
          />
        </div>
        <div className="composer-shell">
          <Input.TextArea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="问一个关于业务数据的问题…"
            autoSize={{ minRows: 1, maxRows: 5 }}
            onPressEnter={(event) => {
              if (!event.shiftKey) {
                event.preventDefault();
                if (isConversationBusy) return;
                submit(input);
                setInput("");
              }
            }}
          />
          <div className="composer-actions">
            <Text type="secondary" style={{ fontSize: 12 }}>Enter 发送 · Shift + Enter 换行</Text>
            {isConversationBusy && active ? (
              <Button
                className="stop-query-button"
                danger
                icon={<StopOutlined />}
                loading={stoppingTraceId === active.trace_id}
                onClick={stopActiveQuery}
                aria-label="停止查询"
              >
                停止查询
              </Button>
            ) : (
              <Button
                className="send-button"
                type="primary"
                shape="circle"
                icon={<SendOutlined />}
                disabled={!input.trim()}
                onClick={() => { submit(input); setInput(""); }}
                aria-label="发送问题"
              />
            )}
          </div>
        </div>
      </main>

    </div>
  );
}
