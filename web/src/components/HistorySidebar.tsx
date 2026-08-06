import { useMemo, useState } from "react";
import { Button, Dropdown, Input, Modal, Tooltip, Typography, message } from "antd";
import {
  CopyOutlined,
  DeleteOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MoreOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import type { QueryRecord } from "../types";

const { Text } = Typography;

interface Props {
  records: QueryRecord[];
  activeTrace: string | null;
  onOpen: (record: QueryRecord) => void;
  onNew: () => void;
  onRefresh: () => void;
  onDelete: (record: QueryRecord) => Promise<void>;
}

const statusLabels: Record<string, string> = {
  done: "已完成",
  running: "处理中",
  pending_review: "待确认",
  error: "失败",
  rejected: "已驳回",
  blocked: "已拦截",
};

export default function HistorySidebar({ records, activeTrace, onOpen, onNew, onRefresh, onDelete }: Props) {
  const [collapsed, setCollapsed] = useState(false);
  const [keyword, setKeyword] = useState("");
  const filtered = useMemo(
    () => records.filter((record) => record.user_query.toLowerCase().includes(keyword.trim().toLowerCase())),
    [records, keyword],
  );

  return (
    <aside className={`history-sidebar ${collapsed ? "collapsed" : ""}`}>
      <div className="history-sidebar-header">
        <Text strong className="history-title">最近对话</Text>
        <Tooltip title={collapsed ? "展开会话列表" : "收起会话列表"}>
          <Button
            type="text"
            shape="circle"
            icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? "展开会话列表" : "收起会话列表"}
          />
        </Tooltip>
      </div>
      <Button className="new-chat-button" icon={<PlusOutlined />} onClick={onNew}>
        <span className="new-chat-label">新建对话</span>
      </Button>
      <div className="history-content">
        <Input
          className="history-search"
          allowClear
          prefix={<SearchOutlined />}
          value={keyword}
          onChange={(event) => setKeyword(event.target.value)}
          placeholder="搜索历史问题"
          suffix={
            <Tooltip title="刷新">
              <ReloadOutlined onClick={onRefresh} style={{ cursor: "pointer" }} />
            </Tooltip>
          }
        />
        <div className="history-list">
          {filtered.map((record) => (
            <div
              key={record.trace_id}
              className={`history-item ${record.trace_id === activeTrace ? "active" : ""}`}
              onClick={() => onOpen(record)}
              role="button"
              tabIndex={0}
              onKeyDown={(event) => event.key === "Enter" && onOpen(record)}
            >
              <div className="history-item-title" title={record.user_query}>{record.user_query}</div>
              <div className="history-item-meta">
                <span>{record.created_at ? dayjs(record.created_at).format("MM-DD HH:mm") : "刚刚"}</span>
                <span>·</span>
                <span>{statusLabels[record.status] || record.status}</span>
              </div>
              <Dropdown
                trigger={["click"]}
                menu={{
                  items: [
                    { key: "open", label: "打开会话" },
                    { key: "copy", icon: <CopyOutlined />, label: "复制 Trace ID" },
                    { type: "divider" },
                    {
                      key: "delete",
                      icon: <DeleteOutlined />,
                      label: "删除会话",
                      danger: true,
                      disabled: ["running", "pending_review"].includes(record.status),
                    },
                  ],
                  onClick: ({ key, domEvent }) => {
                    domEvent.stopPropagation();
                    if (key === "open") onOpen(record);
                    if (key === "copy") {
                      navigator.clipboard.writeText(record.trace_id);
                      message.success("Trace ID 已复制");
                    }
                    if (key === "delete") {
                      Modal.confirm({
                        title: "删除这条对话？",
                        content: "查询记录、反馈和恢复状态将一并删除，且无法撤销。",
                        okText: "删除",
                        cancelText: "取消",
                        okButtonProps: { danger: true },
                        onOk: () => onDelete(record),
                      });
                    }
                  },
                }}
              >
                <Button
                  className="history-item-action"
                  type="text"
                  size="small"
                  icon={<MoreOutlined />}
                  onClick={(event) => event.stopPropagation()}
                  aria-label="会话操作"
                />
              </Dropdown>
            </div>
          ))}
          {filtered.length === 0 && <Text type="secondary">没有匹配的会话</Text>}
        </div>
      </div>
    </aside>
  );
}
