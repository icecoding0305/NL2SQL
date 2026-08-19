import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Card,
  Collapse,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import {
  CheckOutlined,
  CloseOutlined,
  EditOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { apiGet, apiPost } from "../api";
import type { DatabaseConfig } from "../types";

const { Text } = Typography;

interface ColumnInfo {
  name: string;
  type: string;
  comment: string;
  eff_comment: string;
  overridden: boolean;
  sensitive: boolean;
}

interface TableInfo {
  table_name: string;
  comment: string;
  columns: ColumnInfo[];
}

interface ReviewItem {
  id: number;
  datasource: string;
  table_name: string;
  column_name: string | null;
  draft_comment: string;
  status: string;
  draft_confidence?: number;
  validation_errors?: string[];
  reject_reason?: string;
}

// ---------------- 表结构浏览 + 补充注释 ----------------

function SchemaBrowse({ databaseId }: { databaseId?: string }) {
  const [tables, setTables] = useState<TableInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [editTarget, setEditTarget] = useState<{
    table: string;
    column: string | null;
    current: string;
  } | null>(null);
  const [editText, setEditText] = useState("");

  const load = useCallback(async () => {
    if (!databaseId) {
      setTables([]);
      return;
    }
    setLoading(true);
    try {
      const data = await apiGet<TableInfo[]>(`/api/schema?database_id=${encodeURIComponent(databaseId)}`);
      setTables(data);
    } catch (error) {
      setTables([]);
      message.error(error instanceof Error ? error.message : "表结构加载失败");
    } finally {
      setLoading(false);
    }
  }, [databaseId]);

  useEffect(() => {
    load();
  }, [load]);

  const openEdit = (table: string, column: string | null, current: string) => {
    setEditTarget({ table, column, current });
    setEditText(current || "");
  };

  const saveComment = async () => {
    if (!editTarget) return;
    const { table, column } = editTarget;
    const url = column
      ? `/api/schema/${table}/${column}/comment`
      : `/api/schema/${table}/comment`;
    await apiPost(`${url}?database_id=${encodeURIComponent(databaseId || "")}`, { comment: editText });
    message.success("已保存(写入系统覆盖层,不改数据库 DDL)");
    setEditTarget(null);
    load();
  };

  const tableItems = tables.map((t) => ({
    key: t.table_name,
    label: (
      <Space>
        <Text strong>{t.table_name}</Text>
        <Tag>{t.columns.length}列</Tag>
      </Space>
    ),
    children: (
      <div>
        <Space style={{ marginBottom: 8 }}>
          <Text type="secondary">表注释:</Text>
          <Text>{t.comment || "(空)"}</Text>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(t.table_name, null, t.comment)}>
            {t.comment ? "修改" : "补充"}
          </Button>
        </Space>
        <Table
          size="small"
          rowKey="name"
          columns={[
            {
              title: "字段",
              dataIndex: "name",
              width: 200,
              render: (v: string, r: ColumnInfo) => (
                <Space>
                  <Text code>{v}</Text>
                  {r.sensitive && <Tag color="red">敏感</Tag>}
                  {r.overridden && <Tag color="blue">已覆盖</Tag>}
                </Space>
              ),
            },
            { title: "类型", dataIndex: "type", width: 100 },
            {
              title: "注释",
              dataIndex: "eff_comment",
              render: (v: string) => v || <Text type="secondary">(空)</Text>,
            },
            {
              title: "操作",
              width: 100,
              render: (_: unknown, r: ColumnInfo) => (
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(t.table_name, r.name, r.eff_comment)}>
                  {r.eff_comment ? "修改" : "补充"}
                </Button>
              ),
            },
          ]}
          dataSource={t.columns}
          pagination={false}
        />
      </div>
    ),
  }));

  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Text type="secondary">共 {tables.length} 张表。点击"补充/修改"为字段写注释,保存即写入系统覆盖层。</Text>
      </Space>
      {tables.length === 0 && !loading && <Text type="secondary">该数据库暂无已同步的表，请返回数据库连接页面同步 Schema。</Text>}
      <Collapse items={tableItems} ghost={tables.length === 0} />

      <Modal
        title={editTarget ? `补充注释: ${editTarget.column || editTarget.table}` : ""}
        open={!!editTarget}
        onCancel={() => setEditTarget(null)}
        onOk={saveComment}
        okText="保存"
      >
        <Input.TextArea
          rows={3}
          value={editText}
          onChange={(e) => setEditText(e.target.value)}
          placeholder="填写字段/表的注释说明"
        />
      </Modal>
    </div>
  );
}

// ---------------- 待审核注释队列 ----------------

function ReviewQueue({ databaseId }: { databaseId?: string }) {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [target, setTarget] = useState<ReviewItem | null>(null);
  const [editText, setEditText] = useState("");
  const [rejectReason, setRejectReason] = useState("");
  const [mode, setMode] = useState<"approve" | "reject">("approve");
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    if (!databaseId) {
      setItems([]);
      return;
    }
    const data = await apiGet<ReviewItem[]>(
      `/api/schema/review?database_id=${encodeURIComponent(databaseId)}&status=pending`,
    ).catch(() => []);
    setItems(data);
  }, [databaseId]);

  useEffect(() => {
    load();
  }, [load]);

  const openApprove = (r: ReviewItem) => {
    setMode("approve");
    setTarget(r);
    setEditText(r.draft_comment || "");
    setRejectReason("");
  };
  const openReject = (r: ReviewItem) => {
    setMode("reject");
    setTarget(r);
    setRejectReason("");
    setEditText("");
  };

  const submit = async () => {
    if (!target) return;
    if (mode === "approve") {
      await apiPost(`/api/schema/review/${target.id}/approve`, {
        edited_comment: editText,
      });
      message.success("已通过(写入覆盖层)");
    } else {
      if (!rejectReason.trim()) {
        message.warning("驳回必须填写原因");
        return;
      }
      await apiPost(`/api/schema/review/${target.id}/reject`, { reason: rejectReason });
      message.success("已驳回");
    }
    setTarget(null);
    load();
  };

  const reingest = async () => {
    setRefreshing(true);
    const r = await apiPost<{ status: string }>(
      `/api/schema/review/reingest?database_id=${encodeURIComponent(databaseId || "")}`,
    );
    message.success(`已重新入库,更新 schema/m-schema`);
    setRefreshing(false);
    load();
  };

  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Text type="secondary">待审核注释 {items.length} 条(LLM 生成的候选注释,人工确认后生效)。</Text>
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={refreshing}
          onClick={reingest}
        >
          重新入库(应用已确认注释)
        </Button>
      </Space>
      {items.length === 0 ? (
        <Text type="secondary">没有待审核的注释</Text>
      ) : (
        <Table
          size="small"
          rowKey="id"
          dataSource={items}
          pagination={false}
          columns={[
            { title: "ID", dataIndex: "id", width: 60 },
            { title: "表", dataIndex: "table_name", width: 220 },
            {
              title: "字段",
              dataIndex: "column_name",
              width: 160,
              render: (v: string | null) => v || <Tag>表注释</Tag>,
            },
            {
              title: "草稿注释",
              dataIndex: "draft_comment",
              ellipsis: true,
              render: (v: string) => v || <Text type="secondary">(空)</Text>,
            },
            {
              title: "置信度",
              dataIndex: "draft_confidence",
              width: 90,
              render: (v: number | undefined) => (v != null ? v.toFixed(2) : "-"),
            },
            {
              title: "操作",
              width: 150,
              render: (_: unknown, r: ReviewItem) => (
                <Space>
                  <Button size="small" type="primary" icon={<CheckOutlined />} onClick={() => openApprove(r)}>
                    通过
                  </Button>
                  <Button size="small" danger icon={<CloseOutlined />} onClick={() => openReject(r)}>
                    驳回
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      )}

      <Modal
        title={target ? `审核 ${target.table_name}${target.column_name ? "." + target.column_name : "[表]"}` : ""}
        open={!!target}
        onCancel={() => setTarget(null)}
        onOk={submit}
        okText={mode === "approve" ? "通过" : "确认驳回"}
        okButtonProps={{ danger: mode === "reject" }}
      >
        {mode === "approve" ? (
          <Input.TextArea
            rows={3}
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            placeholder="最终注释(可修改草稿)"
          />
        ) : (
          <Input.TextArea
            rows={3}
            value={rejectReason}
            onChange={(e) => setRejectReason(e.target.value)}
            placeholder="驳回原因(必填)"
          />
        )}
      </Modal>
    </div>
  );
}

// ---------------- 页面入口 ----------------

export default function SchemaPage() {
  const [databases, setDatabases] = useState<DatabaseConfig[]>([]);
  const [databaseId, setDatabaseId] = useState<string>();

  useEffect(() => {
    apiGet<DatabaseConfig[]>("/api/databases")
      .then((items) => {
        setDatabases(items);
        const preferred = items.find((item) => item.is_default && item.schema_status === "ready")
          || items.find((item) => item.schema_status === "ready");
        setDatabaseId((current) => (
          current && items.some((item) => item.id === current && item.schema_status === "ready")
            ? current
            : preferred?.id
        ));
      })
      .catch((error) => message.error(error instanceof Error ? error.message : "数据库列表加载失败"));
  }, []);

  return (
    <Card
      title="表结构与注释审核"
      extra={
        <Space>
          <Text>数据库:</Text>
          <Select
            value={databaseId}
            onChange={setDatabaseId}
            placeholder="选择已同步的数据库"
            style={{ width: 240 }}
            options={databases.map((item) => ({
              value: item.id,
              label: `${item.name}${item.is_default ? "（默认）" : ""}`,
              disabled: item.schema_status !== "ready",
            }))}
          />
        </Space>
      }
    >
      <Tabs
        items={[
          { key: "browse", label: "表结构 / 补充字段注释", children: <SchemaBrowse databaseId={databaseId} /> },
          { key: "queue", label: "待审核注释", children: <ReviewQueue databaseId={databaseId} /> },
        ]}
      />
    </Card>
  );
}
