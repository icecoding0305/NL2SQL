import { useEffect, useState } from "react";
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
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  ApiOutlined,
  CloudServerOutlined,
  CloudSyncOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  StarFilled,
  StarOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { apiDelete, apiGet, apiPost, apiPut } from "../api";
import type { DatabaseConfig } from "../types";

const { Text, Title } = Typography;

type DatabaseForm = Omit<DatabaseConfig, "id" | "password_configured" | "schema_status" | "is_default"> & {
  password?: string;
};

const createDefaults: Partial<DatabaseForm> = {
  engine: "mysql",
  port: 3306,
  namespace: "risk_mart",
  password: "",
};

const statusView: Record<DatabaseConfig["schema_status"], { color: string; text: string }> = {
  ready: { color: "success", text: "可查询" },
  syncing: { color: "processing", text: "同步中" },
  not_synced: { color: "warning", text: "待同步" },
  error: { color: "error", text: "同步失败" },
};

export default function DatabasePage() {
  const [items, setItems] = useState<DatabaseConfig[]>([]);
  const [editing, setEditing] = useState<DatabaseConfig | null>(null);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMode, setSaveMode] = useState<"save" | "save_test">("save");
  const [busy, setBusy] = useState<string>();
  const [form] = Form.useForm<DatabaseForm>();

  const load = () => apiGet<DatabaseConfig[]>("/api/databases").then(setItems).catch((error) => {
    message.error(error instanceof Error ? error.message : "数据库配置加载失败");
  });

  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (!items.some((item) => item.schema_status === "syncing")) return;
    const timer = window.setInterval(load, 2500);
    return () => window.clearInterval(timer);
  }, [items]);

  const showCreate = () => {
    setEditing(null);
    setOpen(true);
  };

  const showEdit = (item: DatabaseConfig) => {
    setEditing(item);
    setOpen(true);
  };

  const initializeForm = (visible: boolean) => {
    if (!visible) return;
    form.resetFields();
    form.setFieldsValue(editing ? {
      name: editing.name,
      engine: editing.engine,
      host: editing.host,
      port: editing.port,
      database_name: editing.database_name,
      username: editing.username,
      namespace: editing.namespace,
      password: "",
    } : createDefaults);
  };

  const save = async (testAfterSave = false) => {
    try {
      const values = await form.validateFields();
      setSaveMode(testAfterSave ? "save_test" : "save");
      setSaving(true);
      const saved = editing
        ? await apiPut<DatabaseConfig>(`/api/databases/${editing.id}`, values)
        : await apiPost<DatabaseConfig>("/api/databases", values);
      setItems((current) => {
        const exists = current.some((item) => item.id === saved.id);
        return exists
          ? current.map((item) => item.id === saved.id ? saved : item)
          : [...current, saved];
      });
      setOpen(false);
      message.success(editing ? "数据库配置已更新" : "数据库配置已保存，可测试连接");
      void load();

      if (testAfterSave) {
        setBusy(`${saved.id}:test`);
        try {
          await apiPost(`/api/databases/${saved.id}/test`);
          message.success("配置已保存，数据库连接测试成功");
        } catch (testError) {
          message.warning(
            `配置已保存，但连接测试失败：${testError instanceof Error ? testError.message : "请检查连接参数"}`,
            6,
          );
        } finally {
          setBusy(undefined);
        }
      }
    } catch (error) {
      if (error && typeof error === "object" && "errorFields" in error) {
        message.warning("请先补全必填的数据库连接信息");
        return;
      }
      message.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const action = async (id: string, kind: "test" | "sync" | "default") => {
    setBusy(`${id}:${kind}`);
    try {
      const path = kind === "test" ? "test" : kind === "sync" ? "sync-schema" : "default";
      await apiPost(`/api/databases/${id}/${path}`);
      message.success(kind === "test" ? "连接成功" : kind === "sync" ? "Schema 同步已开始" : "已设为默认数据库");
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(undefined);
    }
  };

  const remove = async (id: string) => {
    try {
      await apiDelete(`/api/databases/${id}`);
      message.success("数据库配置已删除");
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除失败");
    }
  };

  const readyCount = items.filter((item) => item.schema_status === "ready").length;
  const defaultDatabase = items.find((item) => item.is_default);

  return (
    <div className="management-page database-page">
      <section className="database-hero">
        <div className="database-hero-main">
          <span className="database-hero-icon"><CloudServerOutlined /></span>
          <div>
            <Title level={3} style={{ margin: 0 }}>数据库管理</Title>
            <Text type="secondary">连接并管理用于自然语言查询的数据源</Text>
          </div>
        </div>
        <Button type="primary" size="large" icon={<PlusOutlined />} onClick={showCreate}>添加数据库</Button>
        <div className="database-overview">
          <div className="database-overview-item">
            <Text type="secondary">数据源</Text>
            <Text strong>{items.length}</Text>
          </div>
          <div className="database-overview-item">
            <Text type="secondary">可查询</Text>
            <Text strong>{readyCount}</Text>
          </div>
          <div className="database-overview-item database-overview-default">
            <Text type="secondary">默认数据库</Text>
            <Text strong ellipsis={{ tooltip: defaultDatabase?.name }}>{defaultDatabase?.name || "尚未设置"}</Text>
          </div>
        </div>
      </section>
      <Alert
        showIcon
        type="info"
        message="使用流程：保存连接 → 测试连接 → 同步 Schema"
        description="只有 Schema 同步完成的数据源，才会出现在数据问答的数据库选择器中。密码只用于建立连接，不会在页面或接口中回显。"
        className="management-alert database-guide"
      />
      {items.length === 0 ? (
        <Card className="database-empty-card">
          <Empty description="还没有数据库连接">
            <Button type="primary" icon={<PlusOutlined />} onClick={showCreate}>添加第一个数据库</Button>
          </Empty>
        </Card>
      ) : (
        <div className="database-grid">
          {items.map((item) => {
            const status = statusView[item.schema_status];
            return (
              <Card className={`database-card database-card-${item.schema_status}`} key={item.id}>
                <div className="database-card-content">
                  <div className="database-card-head">
                    <div className={`database-engine-mark ${item.engine}`}>
                      <CloudServerOutlined />
                    </div>
                    <div className="database-identity">
                      <Space size={8} wrap>
                        <Text strong className="database-card-title">{item.name}</Text>
                        {item.is_default && <Tag icon={<StarFilled />} color="gold">默认</Tag>}
                      </Space>
                      <Text type="secondary" className="database-endpoint">{item.host}:{item.port}/{item.database_name}</Text>
                    </div>
                    <span className={`database-status database-status-${item.schema_status}`}>
                      <i />{status.text}
                    </span>
                  </div>

                  <div className="database-meta-grid">
                    <div className="database-meta-item">
                      <Text type="secondary">数据库类型</Text>
                      <Text strong>{item.engine === "mysql" ? "MySQL" : "PostgreSQL"}</Text>
                    </div>
                    <div className="database-meta-item">
                      <Text type="secondary">连接用户</Text>
                      <Text strong><UserOutlined /> {item.username}</Text>
                    </div>
                    <div className="database-meta-item">
                      <Text type="secondary">业务空间</Text>
                      <Text strong>{item.namespace}</Text>
                    </div>
                  </div>

                  <div className={`database-schema-note ${item.schema_status}`}>
                    <div>
                      <Text strong>Schema</Text>
                      <Text type={item.schema_status === "error" ? "danger" : "secondary"}>
                        {item.schema_message || (item.schema_status === "ready" ? "结构已同步，可以开始提问" : "连接成功后请同步数据库结构")}
                      </Text>
                    </div>
                  </div>

                  <div className="database-card-actions">
                    <Space wrap>
                      <Button icon={<ApiOutlined />} loading={busy === `${item.id}:test`} onClick={() => action(item.id, "test")}>测试连接</Button>
                      <Button type={item.schema_status === "not_synced" ? "primary" : "default"} icon={<CloudSyncOutlined />} loading={item.schema_status === "syncing" || busy === `${item.id}:sync`} onClick={() => action(item.id, "sync")}>同步 Schema</Button>
                    </Space>
                    <Space size={4}>
                      {!item.is_default && (
                        <Tooltip title="设为默认数据库"><Button type="text" icon={<StarOutlined />} onClick={() => action(item.id, "default")} aria-label="设为默认数据库" /></Tooltip>
                      )}
                      <Tooltip title="编辑连接"><Button type="text" icon={<EditOutlined />} onClick={() => showEdit(item)} aria-label="编辑连接" /></Tooltip>
                      <Popconfirm title="删除这个数据库配置？" description="不会删除业务数据库或已生成的 M-Schema。" onConfirm={() => remove(item.id)}>
                        <Tooltip title="删除连接"><Button type="text" danger icon={<DeleteOutlined />} aria-label="删除连接" /></Tooltip>
                      </Popconfirm>
                    </Space>
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      )}

      <Modal
        className="database-modal"
        width={680}
        title={editing ? "编辑数据库" : "添加数据库"}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => save(false)}
        afterOpenChange={initializeForm}
        confirmLoading={saving}
        footer={[
          <Button key="cancel" onClick={() => setOpen(false)} disabled={saving}>取消</Button>,
          <Button key="save" onClick={() => save(false)} loading={saving && saveMode === "save"}>仅保存</Button>,
          <Button key="save-test" type="primary" icon={<ApiOutlined />} onClick={() => save(true)} loading={saving && saveMode === "save_test"}>保存并测试连接</Button>,
        ]}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="显示名称" rules={[{ required: true, message: "请输入显示名称" }]}><Input placeholder="例如：风控数据集市" /></Form.Item>
          <Row gutter={12}>
            <Col span={12}><Form.Item name="engine" label="数据库类型" rules={[{ required: true }]}><Select options={[{ value: "mysql", label: "MySQL" }, { value: "postgres", label: "PostgreSQL" }]} onChange={(value) => form.setFieldValue("port", value === "mysql" ? 3306 : 5432)} /></Form.Item></Col>
            <Col span={12}><Form.Item name="port" label="端口" rules={[{ required: true }]}><InputNumber min={1} max={65535} style={{ width: "100%" }} /></Form.Item></Col>
          </Row>
          <Form.Item name="host" label="主机地址" rules={[{ required: true, message: "请输入主机地址" }]}><Input placeholder="mysql.example.com" /></Form.Item>
          <Form.Item name="database_name" label="数据库名称" rules={[{ required: true, message: "请输入数据库名称" }]}><Input /></Form.Item>
          <Row gutter={12}>
            <Col span={12}><Form.Item name="username" label="用户名" rules={[{ required: true }]}><Input autoComplete="off" /></Form.Item></Col>
            <Col span={12}>
              <Form.Item
                name="password"
                label={editing ? "新密码（留空则不修改）" : "密码"}
                rules={[
                  ...(editing ? [] : [{ required: true, message: "请输入密码" }]),
                  {
                    validator: async (_, value) => {
                      if (value && value !== value.trim()) {
                        throw new Error("密码首尾包含空格，请删除后重试");
                      }
                    },
                  },
                ]}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="namespace" label="业务命名空间" tooltip="用于数据权限和术语隔离，不会转换成 PLATFORM_CODE 过滤条件" rules={[{ required: true }]}><Input placeholder="risk_mart" /></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
