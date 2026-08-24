import {
  CheckCircleOutlined,
  CodeOutlined,
  ExclamationCircleOutlined,
  RobotOutlined,
  BulbOutlined,
  DatabaseOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { Button, Card, Collapse, Space, Spin, Tag, Typography } from "antd";
import { PIPELINE_NODES } from "../types";

const { Text, Title } = Typography;

interface ThreadSession {
  trace_id: string;
  query: string;
  status: string;
  steps: Record<string, {
    status: "idle" | "running" | "done" | "interrupt" | "error";
    data?: Record<string, unknown>;
    retries: { attempt: number; reason: string }[];
  }>;
  trace_steps?: string[];
  node_latencies?: Record<string, number>;
}

interface Props {
  sessions: ThreadSession[];
  renderStep: (session: ThreadSession, node: string, title: string) => React.ReactNode;
  databaseName?: string;
  onSuggestion?: (question: string) => void;
}

const statusLabel: Record<string, string> = {
  running: "正在分析",
  pending_review: "等待你的确认",
  done: "回答完成",
  error: "处理失败",
  blocked: "请求已拦截",
  rejected: "请求已驳回",
  cancelled: "查询已停止",
};

const stageByNode: Record<string, string> = {
  entry: "正在理解你的问题",
  query_resolution: "正在理解并整理你的问题",
  schema_retrieval: "正在定位相关业务数据",
  clarify_business: "正在确认业务口径",
  clarify_low_confidence: "正在评估数据匹配结果",
  plan_generation: "正在构建查询",
  plan_validation: "正在检查查询逻辑",
  sql_generation: "正在生成查询",
  static_validation: "正在检查查询安全性",
  sensitive_check: "正在检查数据访问风险",
  human_review: "正在等待审批",
  sandbox_execution: "正在执行查询",
  result_interpretation: "正在整理查询结果",
};

const mainNodeTitles: Record<string, string> = {
  query_resolution: "我理解的问题",
  clarify_business: "需要确认",
  clarify_low_confidence: "需要确认",
  human_review: "等待审批",
  sandbox_execution: "查询结果",
  result_interpretation: "结果说明",
};

const technicalNodes = new Set([
  "schema_retrieval",
  "plan_generation",
  "plan_validation",
  "sql_generation",
  "static_validation",
  "sensitive_check",
]);

function ConversationTurn({
  session,
  renderStep,
}: {
  session: ThreadSession;
  renderStep: (node: string, title: string) => React.ReactNode;
}) {
  const runningNode = PIPELINE_NODES.find(({ node }) => session.steps[node]?.status === "running");
  const lastNode = session.trace_steps?.[session.trace_steps.length - 1];
  const runningLabel = stageByNode[runningNode?.node || lastNode || "entry"] || "正在准备查询";
  const mainNodeOrder: Record<string, number> = {
    query_resolution: 0,
    clarify_business: 1,
    clarify_low_confidence: 1,
    human_review: 1,
    result_interpretation: 2,
    sandbox_execution: 3,
  };
  const mainNodes = PIPELINE_NODES.filter(({ node }) => {
    const step = session.steps[node];
    if (!step || !(node in mainNodeTitles)) return false;
    if (["clarify_business", "clarify_low_confidence", "human_review"].includes(node)) {
      return step.status === "interrupt";
    }
    return true;
  }).sort((left, right) => (mainNodeOrder[left.node] ?? 10) - (mainNodeOrder[right.node] ?? 10));
  const detailNodes = PIPELINE_NODES.filter(
    ({ node }) => technicalNodes.has(node) && Boolean(session.steps[node]),
  );
  const totalLatency = Object.values(session.node_latencies || {}).reduce(
    (sum, value) => sum + (Number(value) || 0),
    0,
  );

  return (
    <>
      <div className="message-row user">
        <div className="message-bubble user-bubble">{session.query}</div>
      </div>
      <div className="message-row assistant">
        <div className="message-bubble assistant-bubble">
          <div className="assistant-heading">
            <span className="assistant-avatar"><RobotOutlined /></span>
            <Text strong>数据助手</Text>
            <Tag className="soft-status">{statusLabel[session.status] || session.status}</Tag>
          </div>
          <div className="pipeline-stack">
            {session.status === "running" && (
              <Card className="processing-card" size="small">
                <Space>
                  <Spin size="small" />
                  <div>
                    <Text>{runningLabel}</Text>
                    <div><Text type="secondary">复杂问题可能需要一点时间</Text></div>
                  </div>
                </Space>
              </Card>
            )}
            {session.status === "done" && (
              <div className="completion-line">
                <CheckCircleOutlined />
                <Text type="secondary">
                  查询已完成{totalLatency > 0 ? ` · ${(totalLatency / 1000).toFixed(1)} 秒` : ""}
                </Text>
              </div>
            )}
            {mainNodes.map(({ node }) => renderStep(node, mainNodeTitles[node]))}
            {session.status === "error" && (
              <Card className="pipeline-card" size="small">
                <Space>
                  <ExclamationCircleOutlined style={{ color: "#a65f5f" }} />
                  <Text>查询处理失败，请稍后重试或检查服务状态。</Text>
                </Space>
              </Card>
            )}
            {session.status === "blocked" && (
              <Card className="pipeline-card" size="small">
                <Space>
                  <ExclamationCircleOutlined style={{ color: "#a65f5f" }} />
                  <Text>当前查询未通过安全或权限检查，未执行数据查询。</Text>
                </Space>
              </Card>
            )}
            {session.status === "cancelled" && (
              <Card className="pipeline-card cancelled-card" size="small">
                <Space>
                  <StopOutlined />
                  <Text>查询已停止，不会继续生成或执行 SQL。</Text>
                </Space>
              </Card>
            )}
            {detailNodes.length > 0 && (
              <Collapse
                className="technical-details"
                ghost
                items={[{
                  key: "details",
                  label: (
                    <Space>
                      <CodeOutlined />
                      <Text type="secondary">技术详情</Text>
                      <Text type="secondary">{detailNodes.length} 个内部步骤</Text>
                    </Space>
                  ),
                  children: (
                    <div className="technical-step-list">
                      {detailNodes.map(({ node, title }) => renderStep(node, title))}
                    </div>
                  ),
                }]}
              />
            )}
          </div>
        </div>
      </div>
    </>
  );
}

const starterQuestions = [
  "查询有逾期客户的基本信息",
  "统计每个产品的贷款总金额和平均贷款金额",
  "按累计贷款金额从高到低返回前 10 个客户",
];

export default function ConversationThread({ sessions, renderStep, databaseName, onSuggestion }: Props) {
  if (sessions.length === 0) {
    return (
      <div className="conversation-empty">
        <div className="conversation-empty-content">
          <div className="empty-orb"><RobotOutlined /></div>
          <Title level={2} className="conversation-empty-title">向企业数据提问</Title>
          <Text type="secondary" className="conversation-empty-description">描述你想了解的业务问题，我会理解需求、生成查询并总结结果。</Text>
          <div className="conversation-scope-hint">
            <DatabaseOutlined />
            <span>当前数据源：{databaseName || "请选择可查询数据库"}</span>
          </div>
          <div className="starter-question-list">
            {starterQuestions.map((question) => (
              <Button key={question} className="starter-question" onClick={() => onSuggestion?.(question)}>
                <BulbOutlined />
                <span>{question}</span>
              </Button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="conversation-thread">
      {sessions.map((session) => (
        <ConversationTurn
          key={session.trace_id}
          session={session}
          renderStep={(node, title) => renderStep(session, node, title)}
        />
      ))}
    </div>
  );
}
