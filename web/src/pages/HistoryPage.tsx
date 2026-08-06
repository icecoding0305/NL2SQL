import { useEffect, useState } from "react";
import {
  Button,
  Card,
  DatePicker,
  Descriptions,
  Drawer,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { apiGet } from "../api";
import { PIPELINE_NODES, type QueryRecord } from "../types";
import PipelineStepCard from "../components/PipelineStepCard";
import { stepsFromState, type StepState } from "./QueryPage";
import { buildBaseColumns, StatusTag } from "../components/queryColumns";

const { Text } = Typography;

function exportCsv(rows: QueryRecord[]) {
  const header = ["trace_id", "user_id", "user_query", "data_scope", "status", "retry_count",
    "plan_retry_count", "result_rows", "approved", "approver", "created_at", "finished_at", "generated_sql"];
  const esc = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = rows.map((r) =>
    [
      r.trace_id, r.user_id, r.user_query, (r.data_scope || []).join(","), r.status,
      r.retry_count ?? 0, r.plan_retry_count ?? 0, r.execution_result?.length ?? 0,
      r.approved ?? "", r.approver ?? "", r.created_at ?? "", r.finished_at ?? "", r.generated_sql ?? "",
    ].map(esc).join(","),
  );
  const csv = [header.map(esc).join(","), ...lines].join("\n");
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `nl2sql_audit_${dayjs().format("YYYYMMDD_HHmmss")}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

export default function HistoryPage() {
  const [rows, setRows] = useState<QueryRecord[]>([]);
  const [userId, setUserId] = useState<string>();
  const [businessLine, setBusinessLine] = useState<string>();
  const [range, setRange] = useState<[dayjs.Dayjs | null, dayjs.Dayjs | null] | null>(null);
  const [detail, setDetail] = useState<QueryRecord | null>(null);
  const [open, setOpen] = useState(false);

  const load = async () => {
    const params = new URLSearchParams();
    if (userId) params.set("user_id", userId);
    if (businessLine) params.set("business_line", businessLine);
    if (range?.[0]) params.set("start_date", range[0].format("YYYY-MM-DDT00:00:00"));
    if (range?.[1]) params.set("end_date", range[1].endOf("day").format("YYYY-MM-DDT23:59:59"));
    const list = await apiGet<QueryRecord[]>(`/api/history?${params.toString()}`).catch(() => []);
    setRows(list);
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, businessLine, range]);

  const openDetail = async (rec: QueryRecord) => {
    const full = await apiGet<QueryRecord>(`/api/audit/${rec.trace_id}`).catch(() => rec);
    setDetail(full);
    setOpen(true);
  };

  const columns = [
    ...buildBaseColumns({ openDetail }),
    { title: "重试", dataIndex: "retry_count", width: 70 },
    { title: "结果行数", dataIndex: "execution_result", width: 90, render: (v: unknown[]) => v?.length ?? 0 },
    {
      title: "审批",
      width: 120,
      render: (_: unknown, r: QueryRecord) =>
        r.approved == null ? "-" : `${r.approved ? "通过" : "驳回"}(${r.approver || "-"})`,
    },
  ];

  const detailSteps = detail ? stepsFromState(detail as any) : {};

  return (
    <Card
      title="历史与审计"
      extra={
        <Button icon={<DownloadOutlined />} onClick={() => exportCsv(rows)} disabled={rows.length === 0}>
          导出审计记录(CSV)
        </Button>
      }
    >
      <Space wrap style={{ marginBottom: 12 }}>
        <Input placeholder="用户ID" allowClear style={{ width: 130 }} onChange={(e) => setUserId(e.target.value || undefined)} />
        <Select
          placeholder="系统"
          allowClear
          style={{ width: 140 }}
          options={["risk_mart", "dw", "core"].map((v) => ({ value: v, label: v }))}
          onChange={(v) => setBusinessLine(v || undefined)}
        />
        <DatePicker.RangePicker
          showTime={false}
          onChange={(v) => setRange(v)}
        />
      </Space>
      <Table
        size="small"
        rowKey="trace_id"
        columns={columns}
        dataSource={rows}
        pagination={{ pageSize: 20 }}
        scroll={{ x: true }}
      />

      <Drawer title={`审计详情 ${detail?.trace_id || ""}`} open={open} onClose={() => setOpen(false)} width={680}>
        {detail && (
          <div>
            <Descriptions column={1} size="small" bordered style={{ marginBottom: 12 }}>
              <Descriptions.Item label="trace_id">
                <Text copyable style={{ fontSize: 12 }}>{detail.trace_id}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="用户">{detail.user_id}</Descriptions.Item>
              <Descriptions.Item label="系统">{(detail.data_scope || []).join(",")}</Descriptions.Item>
              <Descriptions.Item label="状态">{detail.status}</Descriptions.Item>
              <Descriptions.Item label="重试次数">SQL 重试 {detail.retry_count ?? 0},计划重试 {detail.plan_retry_count ?? 0}</Descriptions.Item>
              <Descriptions.Item label="返回行数">{detail.execution_result?.length ?? 0}</Descriptions.Item>
              <Descriptions.Item label="审批">{detail.approved == null ? "未触发" : `${detail.approved ? "通过" : "驳回"} / ${detail.approver || "-"}`}</Descriptions.Item>
              <Descriptions.Item label="最终 SQL">
                <pre style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>{detail.generated_sql}</pre>
              </Descriptions.Item>
            </Descriptions>

            <Text strong>各节点耗时:</Text>
            <div style={{ marginBottom: 12 }}>
              {Object.entries(detail.node_latencies || {}).map(([n, ms]) => (
                <Tag key={n} style={{ marginBottom: 4 }}>
                  {n}: {ms}ms
                </Tag>
              ))}
            </div>

            <Text strong>Pipeline 过程:</Text>
            {PIPELINE_NODES.map(({ node, title }) => {
              const step = detailSteps[node];
              if (!step) return null;
              return <PipelineStepCard key={node} node={node} title={title} step={step as StepState} traceId={detail.trace_id} />;
            })}

            {(detail.feedbacks || []).length > 0 && (
              <>
                <Text strong>用户反馈:</Text>
                {(detail.feedbacks || []).map((f) => (
                  <Card key={f.id} size="small" style={{ marginTop: 8 }}>
                    <Tag color="orange">{f.feedback_type}</Tag>
                    <Text>{f.comment}</Text>
                  </Card>
                ))}
              </>
            )}
          </div>
        )}
      </Drawer>
    </Card>
  );
}
