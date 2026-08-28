import { useEffect, useMemo, useState } from "react";
import { Button, Card, Col, Collapse, Descriptions, Dropdown, Empty, Progress, Row, Segmented, Select, Space, Statistic, Table, Tag, Typography, Upload, message } from "antd";
import { DownloadOutlined, ExperimentOutlined, MoreOutlined, UploadOutlined } from "@ant-design/icons";
import { apiDownload, apiGet, apiPost, apiUpload } from "../api";
import type { DatabaseConfig, SchemaEvaluationCase, SchemaEvaluationMetrics, SchemaEvaluationReport, SchemaEvaluationStatus } from "../types";

const { Paragraph, Text, Title } = Typography;
const pct = (value = 0) => `${(value * 100).toFixed(2)}%`;
const suiteNames: Record<string, string> = { governed_metric: "治理指标", entity_attribute: "实体属性", event_disambiguation: "事件区分", time_and_filter: "时间与过滤", multi_table_join: "多表关联", clarification: "主动澄清" };

function MetricCards({ metrics }: { metrics: SchemaEvaluationMetrics }) {
  const cards = [["表召回", metrics.table_recall_at_k], ["字段召回", metrics.column_recall], ["JOIN 准确率", metrics.join_path_accuracy], ["SchemaPlan 精确率", metrics.schema_plan_exact_match], ["澄清准确率", metrics.clarification_accuracy]] as const;
  return <Row gutter={[12, 12]}>{cards.map(([title, value]) => <Col xs={12} md={8} xl={4} key={title}><Card size="small"><Statistic title={title} value={value * 100} precision={2} suffix="%" /></Card></Col>)}</Row>;
}

function CaseDetail({ item }: { item: SchemaEvaluationCase }) {
  return <Descriptions size="small" column={1} bordered>
    <Descriptions.Item label="期望表">{item.expected_tables.join("、") || "无"}</Descriptions.Item><Descriptions.Item label="实际表">{item.predicted_tables.join("、") || "未命中"}</Descriptions.Item>
    <Descriptions.Item label="期望字段">{item.expected_columns.join("、") || "未标注"}</Descriptions.Item><Descriptions.Item label="实际字段">{item.predicted_columns.join("、") || "未命中"}</Descriptions.Item>
    <Descriptions.Item label="期望 JOIN">{item.expected_joins.map((join) => join.join(" = ")).join("；") || "无"}</Descriptions.Item><Descriptions.Item label="实际 JOIN">{item.predicted_joins.map((join) => join.join(" = ")).join("；") || "无"}</Descriptions.Item>
    <Descriptions.Item label="未解决槽位">{item.unresolved_slots.join("、") || "无"}</Descriptions.Item><Descriptions.Item label="置信度">{pct(item.retrieval_confidence)}</Descriptions.Item>
  </Descriptions>;
}

export default function EvaluationPage() {
  const [status, setStatus] = useState<SchemaEvaluationStatus>(); const [report, setReport] = useState<SchemaEvaluationReport | null>(null);
  const [running, setRunning] = useState(false); const [onlyFailed, setOnlyFailed] = useState(false);
  const [mode, setMode] = useState<"baseline" | "online_shadow">("baseline"); const [databases, setDatabases] = useState<DatabaseConfig[]>([]);
  const [databaseId, setDatabaseId] = useState<string>(); const [caseId, setCaseId] = useState<string>();
  const [runScope, setRunScope] = useState<"all" | "single">("all");
  const load = async () => { const query = new URLSearchParams({ mode }); if (databaseId) query.set("database_id", databaseId); const data = await apiGet<SchemaEvaluationStatus>(`/api/schema-evaluation?${query}`); setStatus(data); setReport(data.report); setRunning(data.running); };
  useEffect(() => { apiGet<DatabaseConfig[]>("/api/databases").then((items) => { setDatabases(items); setDatabaseId((value) => value || items.find((item) => item.is_default)?.id || items[0]?.id); }).catch(() => setDatabases([])); }, []);
  useEffect(() => { void load().catch(() => undefined); }, [mode, databaseId]);
  const run = async (single = false) => { if (!databaseId) return void message.warning("请先选择用于评测的数据库 Schema"); if (single && !caseId) return void message.warning("请先选择一条评测用例"); setRunning(true); try { const data = await apiPost<SchemaEvaluationReport>("/api/schema-evaluation/run", { mode, database_id: databaseId, case_id: single ? caseId : undefined }); setReport(data); message.success(single ? "单条评测完成" : "评测集运行完成"); } catch (error) { message.error(error instanceof Error ? error.message : "评测失败"); } finally { setRunning(false); } };
  const cases = useMemo(() => (report?.cases || []).filter((item) => !onlyFailed || !item.passed), [report, onlyFailed]);

  return <div className="management-page evaluation-page">
    <div className="management-page-header"><div><Title level={2}><ExperimentOutlined /> 召回评测</Title><Paragraph type="secondary">验证问题理解与 Schema 召回，不生成或执行 SQL。</Paragraph></div></div>
    <Card size="small" className="evaluation-section evaluation-control-card">
      <div className="evaluation-dataset-line"><Space size={6}><Text strong>{status?.dataset.name || "评测集"}</Text><Tag>{status?.dataset.case_count || 0} 条</Tag></Space>
        <Dropdown trigger={["click"]} dropdownRender={() => <Card size="small"><Space direction="vertical">
          <Upload accept=".xlsx" maxCount={1} showUploadList={false} beforeUpload={async (file) => { try { await apiUpload("/api/schema-evaluation/dataset", file); setCaseId(undefined); setReport(null); await load(); message.success("Excel 评测集已导入"); } catch (error) { message.error(error instanceof Error ? error.message : "导入失败"); } return false; }}><Button type="text" icon={<UploadOutlined />}>上传 Excel</Button></Upload>
          <Button type="text" icon={<DownloadOutlined />} onClick={() => void apiDownload("/api/schema-evaluation/template", "schema-evaluation-template.xlsx")}>下载模板</Button>
        </Space></Card>}><Button type="text" icon={<MoreOutlined />}>数据集</Button></Dropdown>
      </div>
      <div className="evaluation-controls">
        <Segmented value={mode} onChange={(value) => setMode(value as typeof mode)} options={[{ label: "稳定基线", value: "baseline" }, { label: "在线影子", value: "online_shadow" }]} />
        <Select className="evaluation-schema-select" value={databaseId} onChange={setDatabaseId} placeholder="选择数据库 Schema" options={databases.map((item) => ({ value: item.id, label: `${item.name}${item.is_default ? "（默认）" : ""}` }))} />
        <Segmented value={runScope} onChange={(value) => setRunScope(value as typeof runScope)} options={[{ label: "全部", value: "all" }, { label: "单条", value: "single" }]} />
        {runScope === "single" && <Select showSearch optionFilterProp="label" className="evaluation-case-select" value={caseId} onChange={setCaseId} placeholder="选择评测用例" options={(status?.dataset.cases || []).map((item) => ({ value: item.id, label: `${item.id} · ${item.question}` }))} />}
        <Button type="primary" icon={<ExperimentOutlined />} loading={running} onClick={() => void run(runScope === "single")}>开始评测</Button>
      </div>
      <Text type="secondary">{mode === "baseline" ? "使用冻结意图，定位 Schema 召回回归" : "运行真实问题理解链路，可能调用模型"}</Text>
    </Card>

    {report ? <><MetricCards metrics={report.metrics} />
      {report.intent_metrics && <Row gutter={[12, 12]} className="evaluation-section"><Col xs={12} md={6}><Card size="small"><Statistic title="查询类型准确率" value={report.intent_metrics.query_type_accuracy * 100} precision={2} suffix="%" /></Card></Col><Col xs={12} md={6}><Card size="small"><Statistic title="Intent 槽位召回" value={report.intent_metrics.slot_recall * 100} precision={2} suffix="%" /></Card></Col></Row>}
      <Collapse className="evaluation-section" ghost items={[{ key: "suite", label: "查看分套件表现", children: <Row gutter={[14, 14]}>{Object.entries(report.metrics_by_suite).map(([suite, metric]) => <Col xs={24} md={12} xl={8} key={suite}><Text strong>{suiteNames[suite] || suite}</Text><Progress percent={Number((metric.schema_plan_exact_match * 100).toFixed(2))} /><Text type="secondary">表 {pct(metric.table_recall_at_k)} · 字段 {pct(metric.column_recall)} · JOIN {pct(metric.join_path_accuracy)}</Text></Col>)}</Row> }]} />
      <Card title={<Space>逐题结果<Tag color={report.cases.every((item) => item.passed) ? "green" : "orange"}>{report.cases.filter((item) => item.passed).length}/{report.cases.length} 通过</Tag></Space>} extra={<Button onClick={() => setOnlyFailed((value) => !value)}>{onlyFailed ? "显示全部" : "只看失败"}</Button>}>
        <Table<SchemaEvaluationCase> rowKey="id" dataSource={cases} pagination={{ pageSize: 10 }} expandable={{ expandedRowRender: (item) => <CaseDetail item={item} /> }} columns={[{ title: "结果", width: 76, render: (_, item) => <Tag color={item.passed ? "green" : "red"}>{item.passed ? "通过" : "失败"}</Tag> }, { title: "套件", dataIndex: "suite", width: 120, render: (value: string) => suiteNames[value] || value }, { title: "问题", dataIndex: "question" }, { title: "计划表", dataIndex: "planned_tables", render: (values: string[]) => <Space wrap>{values.map((value) => <Tag key={value}>{value}</Tag>)}</Space> }, { title: "置信度", dataIndex: "retrieval_confidence", width: 100, render: (value: number) => pct(value) }]} />
      </Card></> : <Card><Empty description="尚无评测报告，请上传评测集并运行" /></Card>}
  </div>;
}
