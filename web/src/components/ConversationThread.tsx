import {
  CheckCircleOutlined,
  CodeOutlined,
  ExclamationCircleOutlined,
  RobotOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { Card, Collapse, Space, Spin, Tag, Typography } from "antd";
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
  const mainNodes = PIPELINE_NODES.filter(({ node }) => {
    const step = session.steps[node];
    if (!step || !(node in mainNodeTitles)) return false;
    if (["clarify_business", "clarify_low_confidence", "human_review"].includes(node)) {
      return step.status === "interrupt";
    }
    return true;
  });
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

export default function ConversationThread({ sessions, renderStep }: Props) {
  if (sessions.length === 0) {
    return (
      <div className="conversation-empty">
        <div>
          <div className="empty-orb"><RobotOutlined /></div>
          <Title level={3} style={{ marginBottom: 8 }}>今天想了解哪些数据？</Title>
          <Text type="secondary">用自然语言提问，我会先说明理解，再返回结果。</Text>
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
