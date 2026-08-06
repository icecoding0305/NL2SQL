import { useCallback, useEffect, useState } from "react";
import {
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

const { Text } = Typography;

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

  const refresh = useCallback(async () => {
    const list = await apiGet<QueryRecord[]>("/api/approvals").catch(() => []);
    setItems(list);
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

  return (
    <Card title={`审批队列(${items.length})`}>
      {items.length === 0 ? (
        <Empty description="没有待审批的敏感查询" />
      ) : (
        <Table size="small" rowKey="trace_id" columns={columns} dataSource={items} pagination={false} />
      )}

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
    </Card>
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
