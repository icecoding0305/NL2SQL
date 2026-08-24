import { Space, Tag, Typography } from "antd";
import dayjs from "dayjs";
import type { QueryRecord } from "../types";

const { Text } = Typography;

/** 状态语义色(与主题莫兰迪一致):done=灰绿 / pending_review=土橙 / blocked/error=砖红 / 其它灰 */
export const statusColor = (status: string): string => {
  switch (status) {
    case "done":
      return "green";
    case "pending_review":
      return "orange";
    case "blocked":
    case "error":
      return "red";
    default:
      return "default";
  }
};

const statusLabel: Record<string, string> = {
  running: "执行中",
  done: "已完成",
  pending_review: "待审批",
  error: "执行失败",
  rejected: "已驳回",
  blocked: "已阻断",
  cancelled: "已取消",
};

export const StatusTag = ({ status }: { status: string }) => (
  <Tag color={statusColor(status)}>{statusLabel[status] || status}</Tag>
);

/**
 * 查询列表公共列配置:审批队列与历史审计共用同一视觉语言。
 * 统一列顺序:时间 / 用户 / 系统 / 问题 / 状态。
 */
export function buildBaseColumns({
  openDetail,
}: {
  openDetail: (rec: QueryRecord) => void;
}) {
  return [
    {
      title: "时间",
      dataIndex: "created_at",
      width: 150,
      render: (v: string) => dayjs(v).format("MM-DD HH:mm:ss"),
    },
    { title: "用户", dataIndex: "user_id", width: 90 },
    {
      title: "业务范围",
      dataIndex: "data_scope",
      width: 110,
      render: (v: string[]) => (v || []).join(","),
    },
    {
      title: "问题",
      dataIndex: "user_query",
      ellipsis: true,
      render: (v: string, r: QueryRecord) => <a onClick={() => openDetail(r)}>{v}</a>,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 120,
      render: (v: string) => <StatusTag status={v} />,
    },
  ];
}

export { Space, Text };
