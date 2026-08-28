import { useEffect, useMemo, useState } from "react";
import {
  Alert, Button, Card, Col, Descriptions, Empty, Progress, Row, Space,
  Statistic, Table, Tag, Typography, message,
} from "antd";
import { ExperimentOutlined, ReloadOutlined } from "@ant-design/icons";
import { apiGet, apiPost } from "../api";
import type {
  SchemaEvaluationCase, SchemaEvaluationMetrics, SchemaEvaluationReport,
  SchemaEvaluationStatus,
} from "../types";

const { Paragraph, Text, Title } = Typography;
const pct = (value = 0) => `${(value * 100).toFixed(2)}%`;

const suiteNames: Record<string, string> = {
  governed_metric: "治理指标",
  entity_attribute: "实体属性",
  event_disambiguation: "事件区分",
  time_and_filter: "时间与过滤",
  multi_table_join: "多表关联",
  clarification: "主动澄清",
};

function MetricCards({ metrics }: { metrics: SchemaEvaluationMetrics }) {
  const cards = [
    ["表召回", metrics.table_recall_at_k],
    ["字段召回", metrics.column_recall],
    ["JOIN 准确率", metrics.join_path_accuracy],
    ["SchemaPlan 精确率", metrics.schema_plan_exact_match],
    ["澄清准确率", metrics.clarification_accuracy],
  ] as const;
  return <Row gutter={[12, 12]}>{cards.map(([title, value]) => (
    <Col xs={12} md={8} xl={4} key={title}>
      <Card size="small"><Statistic title={title} value={value * 100} precision={2} suffix="%" /></Card>
    </Col>
  ))}</Row>;
}

function CaseDetail({ item }: { item: SchemaEvaluationCase }) {
  return <div className="evaluation-case-detail">
    <Descriptions size="small" column={1} bordered>
      <Descriptions.Item label="期望表">{item.expected_tables.join("、") || "无需固定表"}</Descriptions.Item>
      <Descriptions.Item label="实际表">{item.predicted_tables.join("、") || "未命中"}</Descriptions.Item>
      <Descriptions.Item label="期望字段">{item.expected_columns.join("、") || "未标注"}</Descriptions.Item>
      <Descriptions.Item label="期望 JOIN">{item.expected_joins.map((join) => join.join(" = ")).join("；") || "无"}</Descriptions.Item>
      <Descriptions.Item label="实际 JOIN">{item.predicted_joins.map((join) => join.join(" = ")).join("；") || "无"}</Descriptions.Item>
      <Descriptions.Item label="未解决槽位">{item.unresolved_slots.join("、") || "无"}</Descriptions.Item>
      <Descriptions.Item label="置信度">{pct(item.retrieval_confidence)}</Descriptions.Item>
    </Descriptions>
  </div>;
}

export default function EvaluationPage() {
  const [status, setStatus] = useState<SchemaEvaluationStatus>();
  const [report, setReport] = useState<SchemaEvaluationReport | null>(null);
  const [running, setRunning] = useState(false);
  const [onlyFailed, setOnlyFailed] = useState(false);

  const load = async () => {
    const data = await apiGet<SchemaEvaluationStatus>("/api/schema-evaluation");
    setStatus(data);
    setReport(data.report);
    setRunning(data.running);
  };
  useEffect(() => { void load().catch(() => undefined); }, []);

  const run = async () => {
    setRunning(true);
    try {
      const data = await apiPost<SchemaEvaluationReport>("/api/schema-evaluation/run");
      setReport(data);
      message.success("黄金集评测完成");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "评测失败");
    } finally {
      setRunning(false);
    }
  };

  const cases = useMemo(() => (
    (report?.cases || []).filter((item) => !onlyFailed || !item.passed)
  ), [report, onlyFailed]);

  return <div className="management-page evaluation-page">
    <div className="management-page-header">
      <div>
        <Title level={2}><ExperimentOutlined /> Schema 召回评测</Title>
        <Paragraph type="secondary">运行生产 Schema 检索节点，检查术语、字段、最小关系子图和澄清决策；不会执行业务 SQL。</Paragraph>
      </div>
      <Space>
        <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>
        <Button type="primary" icon={<ExperimentOutlined />} loading={running} onClick={() => void run()}>运行黄金集</Button>
      </Space>
    </div>

    <Alert type="info" showIcon message={`黄金集 v${status?.dataset.version || report?.dataset_version || "-"} · ${status?.dataset.case_count || report?.cases.length || 0} 条`}
      description={status?.dataset.description || report?.description || "点击运行黄金集建立当前基线。"} />

    {report ? <>
      <MetricCards metrics={report.metrics} />
      <Card title="分套件表现" className="evaluation-section">
        <Row gutter={[14, 14]}>{Object.entries(report.metrics_by_suite).map(([suite, metric]) => (
          <Col xs={24} md={12} xl={8} key={suite}>
            <Text strong>{suiteNames[suite] || suite}</Text>
            <Progress percent={Number((metric.schema_plan_exact_match * 100).toFixed(2))} status={metric.schema_plan_exact_match >= .9 ? "success" : "normal"} />
            <Text type="secondary">表 {pct(metric.table_recall_at_k)} · 字段 {pct(metric.column_recall)} · JOIN {pct(metric.join_path_accuracy)}</Text>
          </Col>
        ))}</Row>
      </Card>
      <Card title={<Space>逐题结果<Tag color={report.cases.every((item) => item.passed) ? "green" : "orange"}>{report.cases.filter((item) => item.passed).length}/{report.cases.length} 通过</Tag></Space>}
        extra={<Button onClick={() => setOnlyFailed((value) => !value)}>{onlyFailed ? "显示全部" : "只看失败"}</Button>}>
        <Table<SchemaEvaluationCase> rowKey="id" dataSource={cases} pagination={{ pageSize: 10 }}
          expandable={{ expandedRowRender: (item) => <CaseDetail item={item} /> }}
          columns={[
            { title: "结果", width: 76, render: (_, item) => <Tag color={item.passed ? "green" : "red"}>{item.passed ? "通过" : "失败"}</Tag> },
            { title: "套件", dataIndex: "suite", width: 120, render: (value: string) => suiteNames[value] || value },
            { title: "问题", dataIndex: "question" },
            { title: "计划表", dataIndex: "planned_tables", render: (values: string[]) => <Space wrap>{values.map((value) => <Tag key={value}>{value}</Tag>)}</Space> },
            { title: "置信度", dataIndex: "retrieval_confidence", width: 100, render: (value: number) => pct(value) },
          ]} />
      </Card>
    </> : <Card><Empty description="尚无评测报告，请运行黄金集" /></Card>}
  </div>;
}
