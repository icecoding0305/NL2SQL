import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Input,
  List,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { CheckOutlined, CloseOutlined, ClockCircleOutlined } from "@ant-design/icons";
import { apiGet, apiPost } from "../api";
import { PIPELINE_NODES, type QueryRecord } from "../types";
import { StepCard, stepsFromState, type StepState } from "./QueryPage";
import { buildBaseColumns, StatusTag } from "../components/queryColumns";

import { ReloadOutlined, SafetyCertificateOutlined } from "@ant-design/icons";

const { Text, Title } = Typography;

function waitText(createdAt?: string) {
  if (!createdAt) return "未知";
  const secs = Math.max(0, (Date.now() - new Date(createdAt).getTime()) / 1000);
  if (secs < 60) return `${Math.floor(secs)} 秒`;
  if (secs < 3600) return `${Math.floor(secs / 60)} 分钟`;
  return `${Math.floor(secs / 3600)} 小时`;
}

const URGENT_SECONDS = 10 * 60; // 超过 10 分钟标记紧急

export default function ApprovalsPage() {
  const [items, setItems] = useState<QueryRecord[]>([]);
  const [detail, setDetail] = useState<QueryRecord | null>(null);
  const [open, setOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [target, setTarget] = useState<QueryRecord | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true);
    try {
      const list = await apiGet<QueryRecord[]>("/api/approvals").catch(() => []);
      setItems(list);
    } finally {
      if (manual) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, [refresh]);

  const openDetail = async (rec: QueryRecord) => {
    const full = await apiGet<QueryRecord>(`/api/query/${rec.trace_id}`).catch(() => rec);
    setDetail(full);
    setOpen(true);
  };

  const doApprove = async (approved: boolean) => {
    if (!target) return;
    if (!approved && !rejectReason.trim()) {
      message.warning("驳回必须填写原因(将作为反馈闭环输入)");
      return;
    }
    setLoading(true);
    try {
      await apiPost(`/api/query/${target.trace_id}/approve`, {
        approved,
        reason: rejectReason,
        approver: "risk_admin",
      });
      message.success(approved ? "已通过,查询将自动继续执行" : "已驳回,原因已记录");
      setTarget(null);
      setRejectReason("");
      refresh();
      if (detail?.trace_id === target.trace_id) {
        setTimeout(async () => {
          const updated = await apiGet<QueryRecord>(`/api/query/${target.trace_id}`).catch(() => null);
          if (updated) setDetail(updated);
        }, 2000);
      }
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const columns = [
    ...buildBaseColumns({ openDetail }),
    {
      title: "命中敏感规则",
      dataIndex: "sensitive_reasons",
      render: (reasons: string[]) => (
        <Space wrap>
          {(reasons || []).map((r) => (
            <Tag key={r} color="red">
              {r}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: "已等待",
      width: 110,
      render: (_: unknown, r: QueryRecord) => {
        const secs = r.created_at ? (Date.now() - new Date(r.created_at).getTime()) / 1000 : 0;
        return (
          <Tag icon={<ClockCircleOutlined />} color={secs > URGENT_SECONDS ? "red" : "orange"}>
            {waitText(r.created_at)}
            {secs > URGENT_SECONDS && " ⚠紧急"}
          </Tag>
        );
      },
    },
    {
      title: "操作",
      width: 180,
      render: (_: unknown, r: QueryRecord) => (
        <Space>
          <Button size="small" type="primary" icon={<CheckOutlined />} onClick={() => setTarget(r)}>
            通过
          </Button>
          <Button size="small" danger icon={<CloseOutlined />} onClick={() => setTarget(r)}>
            驳回
          </Button>
        </Space>
      ),
    },
  ];

  const urgentCount = items.filter((item) => (
    item.created_at
      ? (Date.now() - new Date(item.created_at).getTime()) / 1000 > URGENT_SECONDS
      : false
  )).length;

  return (
    <div className="management-page approval-page">
      <div className="management-page-header">
        <div>
          <Space size={10}>
            <span className="page-icon approval-page-icon"><SafetyCertificateOutlined /></span>
            <Title level={3} style={{ margin: 0 }}>查询审批</Title>
          </Space>
          <Text type="secondary">审核命中敏感规则的查询，查看完整生成过程并决定是否继续执行。</Text>
        </div>
        <Button icon={<ReloadOutlined />} loading={refreshing} onClick={() => void refresh(true)}>
          刷新队列
        </Button>
      </div>

      <div className="approval-summary-grid">
        <Card className="approval-summary-card">
          <Text type="secondary">待审批</Text>
          <Text strong>{items.length}</Text>
        </Card>
        <Card className={`approval-summary-card ${urgentCount > 0 ? "urgent" : ""}`}>
          <Text type="secondary">等待超过 10 分钟</Text>
          <Text strong>{urgentCount}</Text>
        </Card>
      </div>

      <Alert
        className="management-alert"
        type={urgentCount > 0 ? "warning" : "info"}
        showIcon
        message={urgentCount > 0 ? `有 ${urgentCount} 条查询等待时间较长` : "审批队列每 10 秒自动更新"}
        description="通过后查询会从中断点继续执行；驳回原因会进入反馈闭环，帮助后续修正规则和术语映射。"
      />

      <Card className="management-card approval-list-card" title="待处理查询">
        {items.length === 0 ? (
          <div className="approval-empty-state"><Empty description="没有待审批的敏感查询" /></div>
        ) : (
          <Table
            rowKey="trace_id"
            columns={columns}
            dataSource={items}
            pagination={{ pageSize: 10, hideOnSinglePage: true }}
            scroll={{ x: 1080 }}
            rowClassName={(record) => record.created_at
              && (Date.now() - new Date(record.created_at).getTime()) / 1000 > URGENT_SECONDS
              ? "approval-row-urgent" : ""}
          />
        )}
      </Card>

      {/* 审批确认:驳回必须填原因 */}
      <Modal
        open={!!target}
        title={target ? `审批:${target.user_query}` : ""}
        onCancel={() => setTarget(null)}
        onOk={() => doApprove(true)}
        confirmLoading={loading}
        okText="通过并执行"
        footer={
          <Space>
            <Button danger loading={loading} onClick={() => doApprove(false)}>
              驳回(需填原因)
            </Button>
            <Button type="primary" loading={loading} onClick={() => doApprove(true)}>
              通过并执行
            </Button>
            <Button onClick={() => setTarget(null)}>取消</Button>
          </Space>
        }
      >
        <Input.TextArea
          rows={3}
          value={rejectReason}
          onChange={(e) => setRejectReason(e.target.value)}
          placeholder="驳回时必填原因,将写回后端作为反馈闭环输入(用于修正术语映射或规则)"
        />
      </Modal>

      {/* 审批详情:完整 pipeline 过程 */}
      <Drawer
        className="approval-detail-drawer"
        title={`查询详情 ${detail?.trace_id || ""}`}
        open={open}
        onClose={() => setOpen(false)}
        width={640}
      >
        {detail && (
          <div>
            <Card size="small" style={{ marginBottom: 8 }}>
              <Space wrap>
                <Text strong>Q:</Text>
                <Text>{detail.user_query}</Text>
                <StatusTag status={detail.status} />
                {detail.approved !== null && detail.approved !== undefined && (
                  <Tag color={detail.approved ? "green" : "red"}>
                    已{detail.approved ? "通过" : "驳回"}({detail.approver || "-"})
                  </Tag>
                )}
              </Space>
            </Card>
            <ApprovalSteps record={detail} />
          </div>
        )}
      </Drawer>
    </div>
  );
}

// 从 record 恢复各步骤渲染(复用主查询页的 StepCard)
function ApprovalSteps({ record }: { record: QueryRecord }) {
  const steps = stepsFromState(record as any);
  return (
    <div>
      {PIPELINE_NODES.map(({ node, title }) => {
        const step = steps[node];
        if (!step) return null;
        return <StepCard key={node} node={node} title={title} step={step as StepState} traceId={record.trace_id} />;
      })}
    </div>
  );
}
