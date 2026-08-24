import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  Collapse,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import {
  ClockCircleOutlined,
  CodeOutlined,
  DownloadOutlined,
  FilterOutlined,
  HistoryOutlined,
  ReloadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { apiGet } from "../api";
import { PIPELINE_NODES, type QueryRecord } from "../types";
import PipelineStepCard from "../components/PipelineStepCard";
import { stepsFromState, type StepState } from "./QueryPage";
import { buildBaseColumns, StatusTag } from "../components/queryColumns";

const { Text, Title, Paragraph } = Typography;

function exportCsv(rows: QueryRecord[]) {
  const header = ["trace_id", "user_id", "user_query", "data_scope", "status", "retry_count",
    "plan_retry_count", "result_rows", "approved", "approver", "created_at", "finished_at", "generated_sql"];
  const esc = (value: unknown) => `"${String(value ?? "").replace(/"/g, '""')}"`;
  const lines = rows.map((row) => [
    row.trace_id, row.user_id, row.user_query, (row.data_scope || []).join(","), row.status,
    row.retry_count ?? 0, row.plan_retry_count ?? 0, row.execution_result?.length ?? 0,
    row.approved ?? "", row.approver ?? "", row.created_at ?? "", row.finished_at ?? "", row.generated_sql ?? "",
  ].map(esc).join(","));
  const csv = [header.map(esc).join(","), ...lines].join("\n");
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `nl2sql_audit_${dayjs().format("YYYYMMDD_HHmmss")}.csv`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function latencyTotal(record: QueryRecord): number {
  return Object.values(record.node_latencies || {}).reduce((sum, value) => sum + Number(value || 0), 0);
}

function formatLatency(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds >= 10000 ? 1 : 2)} s`;
}

export default function HistoryPage() {
  const [rows, setRows] = useState<QueryRecord[]>([]);
  const [userId, setUserId] = useState("");
  const [businessLine, setBusinessLine] = useState<string>();
  const [statusFilter, setStatusFilter] = useState<string>();
  const [keyword, setKeyword] = useState("");
  const [range, setRange] = useState<[dayjs.Dayjs | null, dayjs.Dayjs | null] | null>(null);
  const [detail, setDetail] = useState<QueryRecord | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [scopeOptions, setScopeOptions] = useState<string[]>([]);

  const load = async (reset = false) => {
    const params = new URLSearchParams();
    if (!reset && userId.trim()) params.set("user_id", userId.trim());
    if (!reset && businessLine) params.set("business_line", businessLine);
    if (!reset && range?.[0]) params.set("start_date", range[0].format("YYYY-MM-DDT00:00:00"));
    if (!reset && range?.[1]) params.set("end_date", range[1].endOf("day").format("YYYY-MM-DDT23:59:59"));
    setLoading(true);
    try {
      const list = await apiGet<QueryRecord[]>(`/api/history?${params.toString()}`);
      setRows(list);
      setScopeOptions((current) => Array.from(new Set([
        ...current,
        ...list.flatMap((item) => item.data_scope || []),
      ])).sort());
    } catch (error) {
      message.error(error instanceof Error ? error.message : "历史记录加载失败");
      setRows([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const resetFilters = () => {
    setUserId("");
    setBusinessLine(undefined);
    setStatusFilter(undefined);
    setKeyword("");
    setRange(null);
    void load(true);
  };

  const visibleRows = useMemo(() => rows.filter((row) => {
    if (statusFilter && row.status !== statusFilter) return false;
    const normalizedKeyword = keyword.trim().toLowerCase();
    if (normalizedKeyword && !`${row.user_query} ${row.generated_sql || ""}`.toLowerCase().includes(normalizedKeyword)) {
      return false;
    }
    return true;
  }), [rows, statusFilter, keyword]);

  const summary = useMemo(() => {
    const completed = visibleRows.filter((item) => item.status === "done").length;
    const failed = visibleRows.filter((item) => ["error", "blocked", "cancelled", "rejected"].includes(item.status)).length;
    const latencies = visibleRows.map(latencyTotal).filter((value) => value > 0);
    const averageLatency = latencies.length
      ? latencies.reduce((sum, value) => sum + value, 0) / latencies.length
      : 0;
    return { total: visibleRows.length, completed, failed, averageLatency };
  }, [visibleRows]);

  const openDetail = async (record: QueryRecord) => {
    const full = await apiGet<QueryRecord>(`/api/audit/${record.trace_id}`).catch(() => record);
    setDetail(full);
    setOpen(true);
  };

  const columns = [
    ...buildBaseColumns({ openDetail }),
    { title: "耗时", width: 90, render: (_: unknown, record: QueryRecord) => formatLatency(latencyTotal(record)) },
    { title: "重试", dataIndex: "retry_count", width: 70 },
    { title: "结果行数", dataIndex: "execution_result", width: 90, render: (value: unknown[]) => value?.length ?? 0 },
    {
      title: "审批",
      width: 120,
      render: (_: unknown, record: QueryRecord) => record.approved == null
        ? <Text type="secondary">未触发</Text>
        : <Tag color={record.approved ? "success" : "error"}>{record.approved ? "已通过" : "已驳回"}</Tag>,
    },
  ];

  const detailSteps = detail ? stepsFromState(detail as any) : {};

  return (
    <div className="management-page audit-page">
      <div className="management-page-header">
        <div>
          <Space size={10}>
            <span className="page-icon audit-page-icon"><HistoryOutlined /></span>
            <Title level={3} style={{ margin: 0 }}>历史与审计</Title>
          </Space>
          <Text type="secondary">追踪查询执行结果、响应耗时和生成过程，支持按条件检索与审计导出。</Text>
        </div>
        <Space wrap>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>刷新</Button>
          <Button type="primary" icon={<DownloadOutlined />} onClick={() => exportCsv(visibleRows)} disabled={visibleRows.length === 0}>
            导出当前结果
          </Button>
        </Space>
      </div>

      <div className="audit-summary-grid">
        <Card className="audit-summary-card"><Text type="secondary">查询记录</Text><Text strong>{summary.total}</Text></Card>
        <Card className="audit-summary-card success"><Text type="secondary">执行成功</Text><Text strong>{summary.completed}</Text></Card>
        <Card className="audit-summary-card error"><Text type="secondary">异常终止</Text><Text strong>{summary.failed}</Text></Card>
        <Card className="audit-summary-card"><Text type="secondary">平均处理耗时</Text><Text strong>{formatLatency(summary.averageLatency)}</Text></Card>
      </div>

      <Card className="audit-filter-card">
        <div className="audit-filter-title"><FilterOutlined /><Text strong>筛选条件</Text></div>
        <div className="audit-filter-grid">
          <Input value={userId} placeholder="用户 ID（精确匹配）" allowClear onChange={(event) => setUserId(event.target.value)} />
          <Select
            value={businessLine}
            placeholder="业务范围"
            allowClear
            options={scopeOptions.map((value) => ({ value, label: value }))}
            onChange={setBusinessLine}
          />
          <Select
            value={statusFilter}
            placeholder="执行状态"
            allowClear
            options={[
              { value: "done", label: "已完成" },
              { value: "running", label: "执行中" },
              { value: "pending_review", label: "待审批" },
              { value: "error", label: "执行失败" },
              { value: "blocked", label: "已阻断" },
              { value: "cancelled", label: "已取消" },
            ]}
            onChange={setStatusFilter}
          />
          <DatePicker.RangePicker value={range} showTime={false} onChange={(value) => setRange(value)} />
          <Input
            value={keyword}
            prefix={<SearchOutlined />}
            placeholder="搜索问题或 SQL"
            allowClear
            onChange={(event) => setKeyword(event.target.value)}
          />
          <Space className="audit-filter-actions">
            <Button type="primary" icon={<SearchOutlined />} loading={loading} onClick={() => void load()}>查询</Button>
            <Button onClick={resetFilters}>重置</Button>
          </Space>
        </div>
      </Card>

      <Card className="management-card audit-list-card" title={`审计记录 · ${visibleRows.length}`}>
        {visibleRows.length === 0 && !loading ? (
          <div className="audit-empty-state"><Empty description="没有符合当前条件的审计记录" /></div>
        ) : (
          <Table
            loading={loading}
            rowKey="trace_id"
            columns={columns}
            dataSource={visibleRows}
            pagination={{ pageSize: 15, hideOnSinglePage: true, showSizeChanger: false }}
            scroll={{ x: 1180 }}
          />
        )}
      </Card>

      <Drawer
        className="audit-detail-drawer"
        title="审计详情"
        open={open}
        onClose={() => setOpen(false)}
        width={760}
      >
        {detail && (
          <div className="audit-detail-content">
            <Card className="audit-detail-hero">
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <Space wrap><StatusTag status={detail.status} /><Text type="secondary">{dayjs(detail.created_at).format("YYYY-MM-DD HH:mm:ss")}</Text></Space>
                <Title level={4} style={{ margin: 0 }}>{detail.user_query}</Title>
                <Text copyable={{ text: detail.trace_id }} type="secondary">Trace ID：{detail.trace_id}</Text>
              </Space>
            </Card>

            <Descriptions className="audit-detail-meta" column={2} size="small">
              <Descriptions.Item label="用户">{detail.user_id}</Descriptions.Item>
              <Descriptions.Item label="业务范围">{(detail.data_scope || []).join(", ") || "-"}</Descriptions.Item>
              <Descriptions.Item label="处理耗时">{formatLatency(latencyTotal(detail))}</Descriptions.Item>
              <Descriptions.Item label="返回行数">{detail.execution_result?.length ?? 0}</Descriptions.Item>
              <Descriptions.Item label="重试次数">SQL {detail.retry_count ?? 0} / 计划 {detail.plan_retry_count ?? 0}</Descriptions.Item>
              <Descriptions.Item label="审批结果">{detail.approved == null ? "未触发" : `${detail.approved ? "通过" : "驳回"} · ${detail.approver || "-"}`}</Descriptions.Item>
            </Descriptions>

            <Card className="audit-sql-card" title={<Space><CodeOutlined />最终 SQL</Space>}>
              {detail.generated_sql ? (
                <Paragraph copyable={{ text: detail.generated_sql }} className="audit-sql-code">
                  <pre>{detail.generated_sql}</pre>
                </Paragraph>
              ) : <Text type="secondary">本次查询没有生成 SQL</Text>}
            </Card>

            <Card className="audit-latency-card" title={<Space><ClockCircleOutlined />节点耗时</Space>}>
              <Space size={[7, 7]} wrap>
                {Object.entries(detail.node_latencies || {}).length > 0
                  ? Object.entries(detail.node_latencies || {}).map(([node, milliseconds]) => (
                    <Tag key={node}>{node} · {formatLatency(Number(milliseconds))}</Tag>
                  ))
                  : <Text type="secondary">暂无节点耗时记录</Text>}
              </Space>
            </Card>

            <Collapse
              className="audit-pipeline-collapse"
              items={[{
                key: "pipeline",
                label: "查看完整 Pipeline 执行过程",
                children: (
                  <div className="audit-pipeline-steps">
                    {PIPELINE_NODES.map(({ node, title }) => {
                      const step = detailSteps[node];
                      if (!step) return null;
                      return <PipelineStepCard key={node} node={node} title={title} step={step as StepState} traceId={detail.trace_id} />;
                    })}
                  </div>
                ),
              }]}
            />

            {(detail.feedbacks || []).length > 0 && (
              <Card className="audit-feedback-card" title="用户反馈">
                {(detail.feedbacks || []).map((feedback) => (
                  <div className="audit-feedback-item" key={feedback.id}>
                    <Tag color="orange">{feedback.feedback_type}</Tag>
                    <Text>{feedback.comment}</Text>
                  </div>
                ))}
              </Card>
            )}
          </div>
        )}
      </Drawer>
    </div>
  );
}
