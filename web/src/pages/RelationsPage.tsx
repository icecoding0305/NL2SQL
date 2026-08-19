import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
  message,
} from "antd";
import {
  ApartmentOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  SwapRightOutlined,
} from "@ant-design/icons";
import { apiDelete, apiGet, apiPost, apiPut } from "../api";
import type {
  DatabaseConfig,
  DatabaseRelation,
  SchemaOptionTable,
} from "../types";

const { Text, Title } = Typography;

type RelationForm = Omit<
  DatabaseRelation,
  "id" | "database_id" | "created_at" | "updated_at"
>;

const cardinalityLabels: Record<DatabaseRelation["cardinality"], string> = {
  one_to_one: "一对一",
  one_to_many: "一对多",
  many_to_one: "多对一",
  many_to_many: "多对多",
  unknown: "未知",
};

export default function RelationsPage() {
  const [databases, setDatabases] = useState<DatabaseConfig[]>([]);
  const [databaseId, setDatabaseId] = useState<string>();
  const [tables, setTables] = useState<SchemaOptionTable[]>([]);
  const [relations, setRelations] = useState<DatabaseRelation[]>([]);
  const [editing, setEditing] = useState<DatabaseRelation | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<RelationForm>();
  const sourceTable = Form.useWatch("source_table", form);
  const targetTable = Form.useWatch("target_table", form);
  const sourceColumns = Form.useWatch("source_columns", form) || [];
  const targetColumns = Form.useWatch("target_columns", form) || [];

  useEffect(() => {
    apiGet<DatabaseConfig[]>("/api/databases")
      .then((items) => {
        setDatabases(items);
        const preferred = items.find((item) => item.is_default && item.schema_status === "ready")
          || items.find((item) => item.schema_status === "ready");
        setDatabaseId((current) => current || preferred?.id);
      })
      .catch((error) => message.error(error instanceof Error ? error.message : "数据库加载失败"));
  }, []);

  const load = async (id = databaseId) => {
    if (!id) return;
    setLoading(true);
    try {
      const [schemaOptions, relationItems] = await Promise.all([
        apiGet<SchemaOptionTable[]>(`/api/databases/${id}/schema-options`),
        apiGet<DatabaseRelation[]>(`/api/databases/${id}/relations`),
      ]);
      setTables(schemaOptions);
      setRelations(relationItems);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "表关系加载失败");
      setTables([]);
      setRelations([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(databaseId); }, [databaseId]);

  const tableOptions = useMemo(() => tables.map((table) => ({
    value: table.table_name,
    label: table.comment ? `${table.table_name} · ${table.comment}` : table.table_name,
  })), [tables]);

  const columnOptions = (tableName?: string) => (
    tables.find((table) => table.table_name === tableName)?.columns || []
  ).map((column) => ({
    value: column.name,
    label: column.comment ? `${column.name} · ${column.comment}` : column.name,
  }));

  const showCreate = () => {
    setEditing(null);
    setOpen(true);
  };

  const showEdit = (item: DatabaseRelation) => {
    setEditing(item);
    setOpen(true);
  };

  const initializeForm = (visible: boolean) => {
    if (!visible) return;
    form.resetFields();
    form.setFieldsValue(editing ? {
      source_table: editing.source_table,
      source_columns: [...editing.source_columns],
      target_table: editing.target_table,
      target_columns: [...editing.target_columns],
      cardinality: editing.cardinality,
      preferred_join_type: editing.preferred_join_type,
      description: editing.description,
      enabled: editing.enabled,
    } : {
      cardinality: "many_to_one",
      preferred_join_type: "inner",
      enabled: true,
      source_columns: [],
      target_columns: [],
    });
  };

  const save = async () => {
    if (!databaseId) return;
    try {
      const values = await form.validateFields();
      if (values.source_table === values.target_table) {
        message.warning("关系两端不能选择同一张表");
        return;
      }
      if (values.source_columns.length !== values.target_columns.length) {
        message.warning("关系两端的字段数量必须一致");
        return;
      }
      setSaving(true);
      if (editing) {
        await apiPut(`/api/databases/${databaseId}/relations/${editing.id}`, values);
      } else {
        await apiPost(`/api/databases/${databaseId}/relations`, values);
      }
      message.success(editing ? "表关系已更新" : "表关系已添加并立即生效");
      setOpen(false);
      await load(databaseId);
    } catch (error) {
      if (error && typeof error === "object" && "errorFields" in error) {
        message.warning("请先补全关系两端的表和字段");
        return;
      }
      message.error(error instanceof Error ? error.message : "表关系保存失败");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (relationId: string) => {
    if (!databaseId) return;
    try {
      await apiDelete(`/api/databases/${databaseId}/relations/${relationId}`);
      message.success("表关系已删除");
      await load(databaseId);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "表关系删除失败");
    }
  };

  return (
    <div className="management-page relation-page">
      <div className="management-page-header">
        <div>
          <Space size={10}>
            <span className="page-icon"><ApartmentOutlined /></span>
            <Title level={3} style={{ margin: 0 }}>表关系管理</Title>
          </Space>
          <Text type="secondary">维护数据库中未声明外键、但业务上可以确认的 JOIN 关系。</Text>
        </div>
        <Space wrap>
          <Select
            value={databaseId}
            style={{ width: 260 }}
            placeholder="选择数据库"
            onChange={setDatabaseId}
            options={databases.map((item) => ({
              value: item.id,
              label: `${item.name}${item.is_default ? "（默认）" : ""}`,
              disabled: item.schema_status !== "ready",
            }))}
          />
          <Button icon={<ReloadOutlined />} onClick={() => load()} loading={loading}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={showCreate} disabled={!databaseId}>添加关系</Button>
        </Space>
      </div>

      <Alert
        showIcon
        type="info"
        message="这里只配置已经确认的业务关系"
        description="保存后，Schema 检索、最短关联路径、查询计划和 JOIN 校验会立即使用该关系。请不要仅因为字段同名就建立关系。"
        className="management-alert"
      />

      {loading ? (
        <Card className="management-card relation-loading"><Text type="secondary">正在加载表关系…</Text></Card>
      ) : relations.length === 0 ? (
        <Card className="management-card"><Empty description="还没有配置表关系" /></Card>
      ) : (
        <div className="relation-list">
          {relations.map((item) => (
            <Card className={`relation-card ${item.enabled ? "" : "disabled"}`} key={item.id}>
              <div className="relation-card-header">
                <Space wrap>
                  <Tag color={item.enabled ? "success" : "default"}>{item.enabled ? "已启用" : "已停用"}</Tag>
                  <Text type="secondary">{item.description || "人工确认的业务关系"}</Text>
                </Space>
                <Space>
                  <Button type="text" icon={<EditOutlined />} onClick={() => showEdit(item)}>编辑</Button>
                  <Popconfirm title="删除这条表关系？" description="删除后，新的查询计划将不再使用该关系。" onConfirm={() => remove(item.id)}>
                    <Button type="text" danger icon={<DeleteOutlined />}>删除</Button>
                  </Popconfirm>
                </Space>
              </div>
              <div className="relation-flow">
                <div className="relation-endpoint">
                  <Text type="secondary" className="relation-endpoint-label">来源表</Text>
                  <Text strong className="relation-table-name">{item.source_table}</Text>
                </div>
                <div className="relation-connector">
                  <Tag color="blue">{cardinalityLabels[item.cardinality]}</Tag>
                  <span className="relation-arrow"><SwapRightOutlined /></span>
                  <Text type="secondary">{item.preferred_join_type.toUpperCase()} JOIN</Text>
                </div>
                <div className="relation-endpoint">
                  <Text type="secondary" className="relation-endpoint-label">目标表</Text>
                  <Text strong className="relation-table-name">{item.target_table}</Text>
                </div>
              </div>
              <div className="relation-field-map">
                <Text type="secondary" className="relation-field-map-label">字段映射</Text>
                <div className="relation-field-pairs">
                  {item.source_columns.map((column, index) => (
                    <div className="relation-field-pair" key={`${column}-${item.target_columns[index]}`}>
                      <Text code>{column}</Text>
                      <SwapRightOutlined />
                      <Text code>{item.target_columns[index]}</Text>
                    </div>
                  ))}
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      <Modal
        title={editing ? "编辑表关系" : "添加表关系"}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={save}
        afterOpenChange={initializeForm}
        confirmLoading={saving}
        okText="保存并生效"
        cancelText="取消"
        width={720}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <div className="relation-form-grid">
            <div>
              <Form.Item name="source_table" label="来源表" rules={[{ required: true, message: "请选择来源表" }]}>
                <Select className="relation-value-select" showSearch optionFilterProp="label" optionLabelProp="value" options={tableOptions} onChange={() => form.setFieldValue("source_columns", [])} />
              </Form.Item>
              <Form.Item name="source_columns" label="来源字段" rules={[{ required: true, message: "请选择来源字段" }]}>
                <Select className="relation-value-select" mode="multiple" maxTagCount="responsive" maxTagTextLength={22} showSearch optionFilterProp="label" optionLabelProp="value" options={columnOptions(sourceTable)} disabled={!sourceTable} />
              </Form.Item>
            </div>
            <div>
              <Form.Item name="target_table" label="目标表" rules={[{ required: true, message: "请选择目标表" }]}>
                <Select className="relation-value-select" showSearch optionFilterProp="label" optionLabelProp="value" options={tableOptions} onChange={() => form.setFieldValue("target_columns", [])} />
              </Form.Item>
              <Form.Item name="target_columns" label="目标字段" rules={[{ required: true, message: "请选择目标字段" }]}>
                <Select className="relation-value-select" mode="multiple" maxTagCount="responsive" maxTagTextLength={22} showSearch optionFilterProp="label" optionLabelProp="value" options={columnOptions(targetTable)} disabled={!targetTable} />
              </Form.Item>
            </div>
          </div>
          {(sourceColumns.length > 0 || targetColumns.length > 0) && (
            <div className={`relation-mapping-preview ${sourceColumns.length !== targetColumns.length ? "invalid" : ""}`}>
              <Text strong>字段对应预览</Text>
              <Text type="secondary">字段按照选择顺序一一对应</Text>
              {Array.from({ length: Math.max(sourceColumns.length, targetColumns.length) }).map((_, index) => (
                <div className="relation-preview-row" key={index}>
                  <Text code>{sourceColumns[index] || "待选择"}</Text>
                  <SwapRightOutlined />
                  <Text code>{targetColumns[index] || "待选择"}</Text>
                </div>
              ))}
            </div>
          )}
          <Space size={16} align="start" wrap>
            <Form.Item name="cardinality" label="关系基数" rules={[{ required: true }]}>
              <Select style={{ width: 180 }} options={Object.entries(cardinalityLabels).map(([value, label]) => ({ value, label }))} />
            </Form.Item>
            <Form.Item name="preferred_join_type" label="推荐关联方式" rules={[{ required: true }]}>
              <Select style={{ width: 180 }} options={[{ value: "inner", label: "INNER JOIN" }, { value: "left", label: "LEFT JOIN" }]} />
            </Form.Item>
            <Form.Item name="enabled" label="启用状态" valuePropName="checked"><Switch checkedChildren="启用" unCheckedChildren="停用" /></Form.Item>
          </Space>
          <Form.Item name="description" label="业务说明">
            <Input.TextArea rows={3} maxLength={300} showCount placeholder="例如：贷款借据中的客户编号对应个人客户主数据的 ECIF 客户编号" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
