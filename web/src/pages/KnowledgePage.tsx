import { type ReactNode, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import {
  AppstoreOutlined,
  BookOutlined,
  CheckCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  ExperimentOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SwapOutlined,
  TagsOutlined,
} from "@ant-design/icons";
import { apiAdminDelete, apiAdminPost, apiAdminPut, apiGet } from "../api";
import type {
  DatabaseConfig,
  KnowledgeItem,
  KnowledgeStatus,
  KnowledgeSummary,
  KnowledgeType,
  SchemaOptionTable,
} from "../types";

const { Paragraph, Text, Title } = Typography;

type KnowledgeView = "overview" | KnowledgeType;
// The modal renders a type-specific dynamic form; Ant Design requires an open
// value map for setFieldsValue across those mutually exclusive fields.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type FormValues = Record<string, any> & {
  name: string;
  description?: string;
  database_id?: string;
  namespace?: string;
  status: KnowledgeStatus;
  priority?: number;
};

const typeMeta: Record<KnowledgeType, { title: string; description: string; color: string }> = {
  term: { title: "业务名词", description: "定义实体、指标、维度及其物理字段绑定", color: "blue" },
  synonym: { title: "同义表达", description: "维护等价、相关和禁止替换的业务表达", color: "purple" },
  business_rule: { title: "业务规则", description: "维护可执行的状态、指标和默认口径", color: "gold" },
  optimization_case: { title: "优化案例", description: "沉淀经过验证的用户问题与准确 SQL", color: "cyan" },
};

const viewIcons: Record<KnowledgeView, ReactNode> = {
  overview: <AppstoreOutlined />,
  term: <TagsOutlined />,
  synonym: <SwapOutlined />,
  business_rule: <SafetyCertificateOutlined />,
  optimization_case: <ExperimentOutlined />,
};

const statusMeta: Record<KnowledgeStatus, { label: string; color: string }> = {
  draft: { label: "草稿", color: "default" },
  published: { label: "已发布", color: "success" },
  disabled: { label: "已停用", color: "warning" },
};

const relationOptions = [
  ["equivalent", "等价"], ["abbreviation", "简称"], ["broader", "上位概念"],
  ["narrower", "下位概念"], ["related", "相关但不等价"], ["forbidden", "禁止替换"],
].map(([value, label]) => ({ value, label }));

function payloadOf(values: FormValues, type: KnowledgeType): Record<string, unknown> {
  if (type === "term") {
    return {
      bindings: ((values.binding_keys as string[]) || []).map((key) => {
        const separator = key.indexOf(".");
        return { table: key.slice(0, separator), column: key.slice(separator + 1) };
      }),
      composite_metric: Boolean(values.composite_metric),
      aggregation: values.aggregation || null,
    };
  }
  if (type === "synonym") {
    return {
      canonical_term: values.canonical_term,
      aliases: values.aliases || [],
      relation_type: values.relation_type || "equivalent",
    };
  }
  if (type === "business_rule") {
    return {
      rule_type: values.rule_type || "predicate",
      concept: values.concept || values.name,
      aliases: values.aliases || [],
      subject: values.subject || "",
      binding_concept: values.binding_concept || "",
      operator: values.operator || "",
      value: values.value ?? null,
      assumption: values.assumption || values.description || "",
    };
  }
  return {
    case_type: "sql_fallback",
    id: values.case_id || values.user_query || values.name,
    user_query: values.user_query,
    sql: values.sql,
    verified: true,
    enabled: true,
  };
}

function formOf(item: KnowledgeItem): FormValues {
  const payload = item.payload || {};
  const bindings = (payload.bindings as { table: string; column: string }[]) || [];
  const plan = (payload.plan_structure as Record<string, unknown>) || {};
  return {
    name: item.name,
    description: item.description,
    database_id: item.database_id || undefined,
    namespace: item.namespace,
    status: item.status,
    priority: item.priority,
    binding_keys: bindings.map((binding) => `${binding.table}.${binding.column}`),
    composite_metric: payload.composite_metric,
    aggregation: payload.aggregation,
    canonical_term: payload.canonical_term,
    aliases: payload.aliases,
    relation_type: payload.relation_type,
    rule_type: payload.rule_type,
    concept: payload.concept,
    subject: payload.subject,
    binding_concept: payload.binding_concept,
    operator: payload.operator,
    value: payload.value,
    assumption: payload.assumption,
    case_type: payload.case_type,
    case_id: payload.id,
    question_pattern: payload.question_pattern,
    user_query: payload.user_query,
    sql: payload.sql,
    dialect: payload.dialect,
    used_tables: payload.used_tables,
    tags: payload.tags,
    action: (payload.question_skeleton as Record<string, unknown>)?.action,
    operators: plan.operators,
    strategy: plan.strategy,
    output_grain: plan.output_grain,
  };
}

export default function KnowledgePage({ view }: { view: KnowledgeView }) {
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [summary, setSummary] = useState<KnowledgeSummary>();
  const [databases, setDatabases] = useState<DatabaseConfig[]>([]);
  const [databaseFilter, setDatabaseFilter] = useState<string>();
  const [schema, setSchema] = useState<SchemaOptionTable[]>([]);
  const [editing, setEditing] = useState<KnowledgeItem>();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [adminToken, setAdminToken] = useState(() => localStorage.getItem("admin_token") || "");
  const [form] = Form.useForm<FormValues>();
  const formDatabaseId = Form.useWatch("database_id", form) as string | undefined;
  const type = view === "overview" ? undefined : view;

  const load = async () => {
    setLoading(true);
    try {
      const query = new URLSearchParams();
      if (type) query.set("knowledge_type", type);
      if (databaseFilter) query.set("database_id", databaseFilter);
      const [knowledge, overview] = await Promise.all([
        apiGet<KnowledgeItem[]>(`/api/knowledge/items?${query}`),
        apiGet<KnowledgeSummary>(`/api/knowledge/summary${databaseFilter ? `?database_id=${databaseFilter}` : ""}`),
      ]);
      const visibleKnowledge = knowledge.filter((item) => (
        item.knowledge_type !== "optimization_case" || item.payload.case_type === "sql_fallback"
      ));
      setItems(visibleKnowledge);
      if (view === "overview") {
        const hiddenCount = knowledge.length - visibleKnowledge.length;
        setSummary({
          ...overview,
          total: Math.max(0, overview.total - hiddenCount),
          by_type: {
            ...overview.by_type,
            optimization_case: visibleKnowledge.filter((item) => item.knowledge_type === "optimization_case").length,
          },
        });
      } else {
        setSummary(overview);
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : "知识加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    apiGet<DatabaseConfig[]>("/api/databases").then(setDatabases).catch(() => setDatabases([]));
  }, []);
  useEffect(() => { void load(); }, [view, databaseFilter]);
  useEffect(() => {
    if (!formDatabaseId) {
      setSchema([]);
      return;
    }
    apiGet<SchemaOptionTable[]>(`/api/databases/${formDatabaseId}/schema-options`)
      .then(setSchema).catch(() => setSchema([]));
  }, [formDatabaseId]);

  const fieldOptions = useMemo(() => schema.flatMap((table) => table.columns.map((column) => ({
    value: `${table.table_name}.${column.name}`,
    label: `${table.table_name}.${column.name}${column.comment ? ` · ${column.comment}` : ""}`,
  }))), [schema]);
  const showCreate = () => {
    if (!type) return;
    setEditing(undefined);
    form.resetFields();
    form.setFieldsValue({
      status: type === "optimization_case" ? "published" : "draft", priority: 100, namespace: "global",
      database_id: databaseFilter, relation_type: "equivalent",
      rule_type: "predicate", case_type: "sql_fallback", action: "detail",
      output_grain: "record",
    });
    setOpen(true);
  };
  const showEdit = (item: KnowledgeItem) => {
    setEditing(item);
    form.resetFields();
    form.setFieldsValue(formOf(item));
    setOpen(true);
  };

  const save = async () => {
    if (!type) return;
    try {
      const values = await form.validateFields();
      if (adminToken) localStorage.setItem("admin_token", adminToken);
      setSaving(true);
      const isSqlCase = type === "optimization_case";
      const body = {
        knowledge_type: type,
        name: isSqlCase ? String(values.user_query || "").trim().slice(0, 120) : values.name,
        description: isSqlCase ? "已验证 SQL 优化案例" : values.description || "",
        database_id: values.database_id || null,
        namespace: isSqlCase ? "global" : values.namespace || "global",
        status: isSqlCase ? "published" : values.status,
        priority: isSqlCase ? 100 : values.priority || 100,
        payload: payloadOf(values, type),
        created_by: "admin",
      };
      if (editing) await apiAdminPut(`/api/knowledge/items/${editing.id}`, body);
      else await apiAdminPost("/api/knowledge/items", body);
      message.success(isSqlCase || values.status === "published" ? "知识已校验并发布" : "知识草稿已保存");
      setOpen(false);
      await load();
    } catch (error) {
      if (error && typeof error === "object" && "errorFields" in error) return;
      message.error(error instanceof Error ? error.message : "知识保存失败");
    } finally {
      setSaving(false);
    }
  };

  const publish = async (item: KnowledgeItem) => {
    try {
      await apiAdminPost(`/api/knowledge/items/${item.id}/publish`);
      message.success("校验通过，知识已发布并进入运行时作用域");
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "发布失败");
    }
  };
  const remove = async (item: KnowledgeItem) => {
    try {
      await apiAdminDelete(`/api/knowledge/items/${item.id}`);
      message.success("知识记录已删除");
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除失败");
    }
  };

  const title = type ? typeMeta[type].title : "知识概览";
  const columns = [
    {
      title: "知识名称", dataIndex: "name", key: "name",
      render: (name: string, item: KnowledgeItem) => (
        <Space direction="vertical" size={2}>
          <Space><Text strong>{name}</Text><Tag color={typeMeta[item.knowledge_type].color}>{typeMeta[item.knowledge_type].title}</Tag></Space>
          <Text type="secondary" ellipsis style={{ maxWidth: 460 }}>{item.description || "暂无业务解释"}</Text>
        </Space>
      ),
    },
    {
      title: "作用范围", key: "scope", width: 180,
      render: (_: unknown, item: KnowledgeItem) => {
        const database = databases.find((db) => db.id === item.database_id);
        return <Space direction="vertical" size={0}><Text>{database?.name || "全局知识"}</Text><Text type="secondary">{item.namespace}</Text></Space>;
      },
    },
    { title: "版本", dataIndex: "version", width: 76, render: (version: number) => <Text>v{version}</Text> },
    {
      title: "状态", dataIndex: "status", width: 92,
      render: (status: KnowledgeStatus) => <Tag color={statusMeta[status].color}>{statusMeta[status].label}</Tag>,
    },
    {
      title: "操作", key: "actions", width: 220,
      render: (_: unknown, item: KnowledgeItem) => (
        <Space>
          <Button type="text" icon={<EditOutlined />} onClick={() => showEdit(item)}>编辑</Button>
          {item.status !== "published" && <Button type="text" icon={<CheckCircleOutlined />} onClick={() => publish(item)}>发布</Button>}
          <Popconfirm title="删除这条知识？" onConfirm={() => remove(item)}><Button type="text" danger icon={<DeleteOutlined />} /></Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="management-page knowledge-page">
      <div className="management-page-header">
        <div>
          <Space size={10}><span className="page-icon knowledge-page-icon">{viewIcons[view]}</span><Title level={3} style={{ margin: 0 }}>{title}</Title></Space>
          <Text type="secondary">{type ? typeMeta[type].description : "统一管理影响问题理解、Schema Grounding 和查询计划的企业知识。"}</Text>
        </div>
        <Space wrap>
          <Select
            allowClear value={databaseFilter} placeholder="全部数据库（含全局）" style={{ width: 230 }}
            onChange={setDatabaseFilter}
            options={databases.map((database) => ({ value: database.id, label: database.name }))}
          />
          <Input.Password value={adminToken} onChange={(event) => setAdminToken(event.target.value)} placeholder="管理 Token（可选）" style={{ width: 180 }} />
          <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>
          {type && <Button type="primary" icon={<PlusOutlined />} onClick={showCreate}>新增{typeMeta[type].title}</Button>}
        </Space>
      </div>

      <Alert
        className="management-alert"
        showIcon
        type="info"
        message={type === "optimization_case"
          ? "这里只维护经过验证的准确 SQL。系统会自动识别方言和涉及表，并在保存前完成只读与 Schema 校验。"
          : "草稿不会影响在线查询；发布前会校验字段、规则或 SQL 结构。数据库专属知识优先于全局知识。"}
      />

      {view === "overview" && summary && (
        <Row gutter={[16, 16]} className="knowledge-summary-grid">
          <Col xs={24} sm={12} xl={6}><Card><Statistic title={<span className="knowledge-stat-title"><BookOutlined />知识总数</span>} value={summary.total} /></Card></Col>
          {(Object.keys(typeMeta) as KnowledgeType[]).map((kind) => (
            <Col xs={24} sm={12} xl={6} key={kind}><Card><Statistic title={<span className="knowledge-stat-title">{viewIcons[kind]}{typeMeta[kind].title}</span>} value={summary.by_type[kind] || 0} /></Card></Col>
          ))}
        </Row>
      )}

      <Card className="management-card knowledge-list-card" title={view === "overview" ? "最近知识" : type === "optimization_case" ? "准确 SQL 列表" : `${title}列表`}>
        {items.length === 0 && !loading ? <Empty description="暂无知识记录" /> : (
          <Table rowKey="id" loading={loading} columns={columns} dataSource={items} pagination={{ pageSize: 10, showSizeChanger: false }} />
        )}
      </Card>

      <Modal title={editing ? `编辑${title}` : `新增${title}`} open={open} onCancel={() => setOpen(false)} onOk={save} confirmLoading={saving} okText={type === "optimization_case" ? "校验并保存" : "保存"} width={760} destroyOnClose>
        <Form form={form} layout="vertical">
          {type !== "optimization_case" && <>
          <Row gutter={16}>
            <Col span={16}><Form.Item name="name" label="知识名称" rules={[{ required: true, message: "请输入知识名称" }]}><Input placeholder="使用业务人员能够理解的标准名称" /></Form.Item></Col>
            <Col span={8}><Form.Item name="status" label="保存状态" rules={[{ required: true }]}><Select options={Object.entries(statusMeta).map(([value, meta]) => ({ value, label: meta.label }))} /></Form.Item></Col>
          </Row>
          <Form.Item name="description" label="业务解释"><Input.TextArea rows={3} maxLength={500} showCount /></Form.Item>
          <Row gutter={16}>
            <Col span={12}><Form.Item name="database_id" label="作用数据库"><Select allowClear placeholder="留空表示全局" options={databases.map((database) => ({ value: database.id, label: database.name }))} /></Form.Item></Col>
            <Col span={8}><Form.Item name="namespace" label="命名空间"><Input placeholder="global" /></Form.Item></Col>
            <Col span={4}><Form.Item name="priority" label="优先级"><InputNumber min={1} max={999} style={{ width: "100%" }} /></Form.Item></Col>
          </Row>
          </>}

          {type === "term" && <>
            <Form.Item name="binding_keys" label="物理字段绑定" rules={[{ required: true, message: "请选择物理字段" }]} extra={!formDatabaseId ? "请先选择作用数据库，再选择真实字段" : undefined}>
              <Select mode="multiple" showSearch optionFilterProp="label" options={fieldOptions} disabled={!formDatabaseId} placeholder="选择一个或多个 table.column" />
            </Form.Item>
            <Row gutter={16}><Col span={12}><Form.Item name="aggregation" label="默认聚合"><Select allowClear options={["sum", "avg", "count", "count_distinct", "min", "max"].map((value) => ({ value, label: value.toUpperCase() }))} /></Form.Item></Col><Col span={12}><Form.Item name="composite_metric" label="复合指标"><Select options={[{ value: false, label: "否" }, { value: true, label: "是" }]} /></Form.Item></Col></Row>
          </>}

          {type === "synonym" && <>
            <Form.Item name="canonical_term" label="标准词" rules={[{ required: true }]}><Input placeholder="例如：不良贷款" /></Form.Item>
            <Form.Item name="aliases" label="表达列表" rules={[{ required: true }]}><Select mode="tags" tokenSeparators={[",", "，"]} placeholder="输入后按回车" /></Form.Item>
            <Form.Item name="relation_type" label="语义关系" rules={[{ required: true }]}><Select options={relationOptions} /></Form.Item>
          </>}

          {type === "business_rule" && <>
            <Row gutter={16}><Col span={8}><Form.Item name="rule_type" label="规则类型" rules={[{ required: true }]}><Select options={[{ value: "predicate", label: "状态判断" }, { value: "metric", label: "指标公式" }, { value: "default", label: "默认口径" }]} /></Form.Item></Col><Col span={8}><Form.Item name="concept" label="业务概念"><Input /></Form.Item></Col><Col span={8}><Form.Item name="subject" label="主体"><Input placeholder="贷款/客户" /></Form.Item></Col></Row>
            <Form.Item name="aliases" label="触发表达"><Select mode="tags" tokenSeparators={[",", "，"]} /></Form.Item>
            <Form.Item name="binding_concept" label="绑定业务名词"><Input placeholder="例如：逾期本金余额" /></Form.Item>
            <Row gutter={16}><Col span={8}><Form.Item name="operator" label="运算符"><Select allowClear options={[">", ">=", "=", "!=", "<", "<="].map((value) => ({ value }))} /></Form.Item></Col><Col span={8}><Form.Item name="value" label="比较值"><Input /></Form.Item></Col><Col span={8}><Form.Item name="assumption" label="用户可见假设"><Input /></Form.Item></Col></Row>
          </>}

          {type === "optimization_case" && <div className="knowledge-sql-case-form">
            <Form.Item
              name="database_id"
              label="适用数据库"
              rules={[{ required: true, message: "请选择 SQL 所属数据库" }]}
              extra="用于自动校验 SQL 中引用的表和数据库方言"
            >
              <Select placeholder="选择数据库" options={databases.map((database) => ({ value: database.id, label: database.name }))} />
            </Form.Item>
            <Form.Item name="user_query" label="用户问题" rules={[{ required: true, message: "请输入该 SQL 对应的用户问题" }]}>
              <Input.TextArea rows={2} placeholder="例如：统计每个产品的贷款总金额和平均贷款金额" />
            </Form.Item>
            <Form.Item
              name="sql"
              label="准确 SQL"
              rules={[{ required: true, message: "请输入经过验证的只读 SQL" }]}
              extra="仅允许 SELECT 查询；保存成功后会直接作为已验证案例生效"
            >
              <Input.TextArea rows={10} className="knowledge-sql-input" placeholder="SELECT ..." />
            </Form.Item>
          </div>}
          {type !== "optimization_case" && <Paragraph type="secondary">发布会创建新版本；校验失败时记录仍可作为草稿保存。</Paragraph>}
        </Form>
      </Modal>
    </div>
  );
}
