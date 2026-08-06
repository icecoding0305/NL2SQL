import { useEffect, useState } from "react";
import { Alert, Button, Card, Input, Select, Space, Table, Tag, Typography, message } from "antd";
import { SaveOutlined } from "@ant-design/icons";
import { apiGet } from "../api";

const { Text } = Typography;

interface TermEntry {
  business_line: string;
  resolved_fields: string[];
  definition: string;
  composite_metric: boolean;
  aliases?: string[];
}

interface TermMapping {
  business_line: string;
  mapping: Record<string, TermEntry>;
}

const BUSINESS_LINES = ["_global", "weiyedai", "zidong", "weilai"];

export default function ConfigPage() {
  const [line, setLine] = useState("_global");
  const [mapping, setMapping] = useState<Record<string, TermEntry>>({});
  const [adminToken, setAdminToken] = useState<string>(() => localStorage.getItem("admin_token") || "");

  const load = async () => {
    const data = await apiGet<TermMapping>(`/api/config/term-mapping?business_line=${line}`).catch(() => null);
    if (data) setMapping(data.mapping || {});
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [line]);

  const save = async () => {
    if (!adminToken) {
      message.warning("请先填写管理 Token(配置只开放给风控管理角色)");
      return;
    }
    localStorage.setItem("admin_token", adminToken);
    const resp = await fetch(`/api/config/term-mapping?business_line=${line}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-Admin-Token": adminToken },
      body: JSON.stringify(mapping),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      message.error((body as { detail?: string }).detail || `保存失败 HTTP ${resp.status}`);
      return;
    }
    message.success("已保存,术语映射将热更新(无需重启服务)");
  };

  const updateField = (term: string, field: string, value: unknown) => {
    setMapping((prev) => ({ ...prev, [term]: { ...prev[term], [field]: value } }));
  };

  const columns = [
    { title: "术语", dataIndex: "term", width: 140 },
    {
      title: "定义",
      dataIndex: "definition",
      render: (v: string, r: { term: string }) => (
        <Input value={v} onChange={(e) => updateField(r.term, "definition", e.target.value)} />
      ),
    },
    {
      title: "解析字段",
      dataIndex: "resolved_fields",
      render: (v: string[], r: { term: string }) => (
        <Select
          mode="tags"
          value={v}
          style={{ minWidth: 200 }}
          onChange={(val) => updateField(r.term, "resolved_fields", val)}
        />
      ),
    },
    {
      title: "复合口径",
      dataIndex: "composite_metric",
      width: 100,
      render: (v: boolean, r: { term: string }) => (
        <Tag
          color={v ? "orange" : "default"}
          style={{ cursor: "pointer" }}
          onClick={() => updateField(r.term, "composite_metric", !v)}
        >
          {v ? "是" : "否"}
        </Tag>
      ),
    },
  ];

  const rows = Object.entries(mapping).map(([term, e]) => ({ term, ...e }));

  return (
    <Card title="配置管理(术语映射)">
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="只开放给风控管理角色。修改会热更新,无需重启服务;不影响已加载的规则文件之外的其它配置。"
      />
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          value={line}
          onChange={setLine}
          style={{ width: 180 }}
          options={BUSINESS_LINES.map((b) => ({ value: b, label: b === "_global" ? "全局(通用)" : b }))}
        />
        <Input.Password
          placeholder="管理 Token(HEADER X-Admin-Token)"
          value={adminToken}
          onChange={(e) => setAdminToken(e.target.value)}
          style={{ width: 240 }}
        />
        <Button type="primary" icon={<SaveOutlined />} onClick={save}>
          保存
        </Button>
      </Space>
      <Table
        size="small"
        rowKey="term"
        columns={columns}
        dataSource={rows}
        pagination={false}
        locale={{ emptyText: <Text type="secondary">暂无术语</Text> }}
      />
    </Card>
  );
}
