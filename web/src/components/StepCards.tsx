import { useState } from "react";
import {
  Button,
  Card,
  Collapse,
  Descriptions,
  Divider,
  Input,
  List,
  Modal,
  Space,
  Table,
  Tag,
  Timeline,
  Typography,
} from "antd";
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CopyOutlined,
  ExclamationCircleOutlined,
  LoadingOutlined,
  BulbOutlined,
  InfoCircleOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { apiPost } from "../api";
import type { ResultSummary, SchemaHit, SchemaPlan } from "../types";

const { Text, Paragraph } = Typography;

// ---------------- 通用:节点状态徽标 ----------------

export function StepStatusTag({ status }: { status: string }) {
  if (status === "running")
    return (
      <Tag icon={<LoadingOutlined />} color="processing">
        执行中
      </Tag>
    );
  if (status === "done")
    return (
      <Tag icon={<CheckCircleOutlined />} color="success">
        完成
      </Tag>
    );
  if (status === "interrupt")
    return (
      <Tag icon={<ClockCircleOutlined />} color="warning">
        等待审批
      </Tag>
    );
  if (status === "error")
    return (
      <Tag icon={<ExclamationCircleOutlined />} color="error">
        失败
      </Tag>
    );
  return <Tag>等待</Tag>;
}

// ---------------- SQL 高亮(轻量词法着色,适配浅色主题) ----------------

const SQL_KEYWORDS = new Set([
  "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "GROUP", "ORDER", "HAVING",
  "LIMIT", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "CROSS", "FULL", "ON",
  "AS", "CASE", "WHEN", "THEN", "ELSE", "END", "SUM", "COUNT", "AVG", "MAX",
  "MIN", "DISTINCT", "IN", "IS", "NULL", "BETWEEN", "LIKE", "BY", "DESC",
  "ASC", "OVER", "PARTITION", "COALESCE", "IFNULL", "CAST", "CONVERT", "DATE",
  "EXISTS", "UNION", "ALL", "EXPLAIN",
]);

// 一次遍历切出 token:注释 / 字符串 / 数字 / 反引号标识符 / 关键字 / 普通文本
function tokenizeSql(sql: string): { type: string; value: string }[] {
  const tokens: { type: string; value: string }[] = [];
  const re =
    /(--[^\n]*)|(\/\*[\s\S]*?\*\/)|'(?:[^']|'')*'|"(?:[^"]|"")*"|`[^`]*`|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_]*|\s+|./g;
  for (const m of sql.matchAll(re)) {
    const text = m[0];
    if (text.startsWith("--") || text.startsWith("/*")) {
      tokens.push({ type: "comment", value: text });
    } else if (text.startsWith("'") || text.startsWith('"')) {
      tokens.push({ type: "string", value: text });
    } else if (text.startsWith("`")) {
      tokens.push({ type: "ident", value: text });
    } else if (/^\d/.test(text)) {
      tokens.push({ type: "number", value: text });
    } else if (/^\s/.test(text)) {
      tokens.push({ type: "plain", value: text });
    } else if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(text) && SQL_KEYWORDS.has(text.toUpperCase())) {
      tokens.push({ type: "keyword", value: text });
    } else {
      tokens.push({ type: "plain", value: text });
    }
  }
  return tokens;
}

const TOKEN_COLORS: Record<string, string> = {
  keyword: "#2563eb",   // 品牌蓝
  string: "#16a34a",    // 绿
  number: "#d97706",    // 琥珀
  ident: "#0f766e",     // 反引号表/字段名(teal)
  comment: "#98a2b3",   // 灰斜体
  plain: "#24292f",     // 主文本
};

export function SqlHighlight({ sql }: { sql: string }) {
  if (!sql) return <span style={{ color: "#98a2b3" }}>—</span>;
  const tokens = tokenizeSql(sql);
  return (
    <pre className="sql-pre">
      {tokens.map((t, i) => (
        <span
          key={i}
          style={{
            color: TOKEN_COLORS[t.type] || TOKEN_COLORS.plain,
            fontWeight: t.type === "keyword" ? 600 : undefined,
            fontStyle: t.type === "comment" ? "italic" : undefined,
          }}
        >
          {t.value}
        </span>
      ))}
    </pre>
  );
}

// ---------------- 模块 3:Schema 检索摘要 ----------------

export function SchemaRetrievalCard({ hits, schemaPlan }: { hits?: SchemaHit[]; schemaPlan?: SchemaPlan | null }) {
  if (!hits || hits.length === 0) return <Text type="secondary">未检索到表</Text>;
  const roles = new Map<string, { label: string; color: string; reason: string }>();
  for (const table of schemaPlan?.anchor_tables || []) {
    roles.set(table.table_name, { label: table.role === "primary_fact" ? "核心事实" : "次级事实", color: "purple", reason: table.reason });
  }
  for (const table of schemaPlan?.dimension_tables || []) {
    roles.set(table.table_name, { label: table.role === "entity" ? "实体输出" : "维度", color: "cyan", reason: table.reason });
  }
  for (const table of schemaPlan?.bridge_tables || []) {
    roles.set(table.table_name, { label: "关联桥接", color: "default", reason: table.reason });
  }
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      {schemaPlan && (
        <Text type="secondary">
          Schema 规划置信度 {(schemaPlan.confidence * 100).toFixed(0)}%
          {schemaPlan.unresolved_slots.length > 0 ? ` · 待确认: ${schemaPlan.unresolved_slots.join("、")}` : ""}
        </Text>
      )}
      <Collapse size="small" items={hits.map((h) => ({
        key: h.table_name,
        label: (
          <Space>
            <Tag color="blue">{h.table_name}</Tag>
            {roles.has(h.table_name) && <Tag color={roles.get(h.table_name)!.color}>{roles.get(h.table_name)!.label}</Tag>}
            {h.business_terms.length > 0 && (
              <Text type="secondary">命中术语: {h.business_terms.join("、")}</Text>
            )}
            {roles.has(h.table_name) && <Text type="secondary">{roles.get(h.table_name)!.reason}</Text>}
          </Space>
        ),
        children: (
          <List
            size="small"
            dataSource={h.columns}
            renderItem={(c) => (
              <List.Item style={{ padding: "2px 0" }}>
                <Text code>{c.name}</Text>
                <Text type="secondary" style={{ marginLeft: 8 }}>
                  {c.comment || c.type}
                </Text>
              </List.Item>
            )}
          />
        ),
      }))} />
    </Space>
  );
}

// ---------------- 模块 5b/6:查询计划卡片 ----------------

export function PlanCard({
  plan,
  traceId,
}: {
  plan: Record<string, any>;
  traceId: string;
}) {
  const [open, setOpen] = useState(false);
  const [comment, setComment] = useState("");

  const sendFeedback = async () => {
    await apiPost("/api/feedback", {
      trace_id: traceId,
      node: "plan_generation",
      feedback_type: "plan_wrong",
      comment,
    });
    setOpen(false);
    setComment("");
  };

  return (
    <>
      <Descriptions column={1} size="small" colon={false}>
        <Descriptions.Item label="目标表">
          {(plan.target_tables || []).map((t: string) => (
            <Tag key={t} color="blue">
              {t}
            </Tag>
          ))}
        </Descriptions.Item>
        {(plan.filters || []).length > 0 && (
          <Descriptions.Item label="过滤条件">
            {plan.filters.map((f: any, i: number) => (
              <div key={i}>
                <Text code>{f.column}</Text> {f.operator} {String(f.value)}
              </div>
            ))}
          </Descriptions.Item>
        )}
        {(plan.join_logic || []).length > 0 && (
          <Descriptions.Item label="关联逻辑">
            {plan.join_logic.map((j: any, i: number) => (
              <div key={i}>
                {j.left_table}.{j.left_column} = {j.right_table}.{j.right_column}
              </div>
            ))}
          </Descriptions.Item>
        )}
        {plan.metric_logic && (
          <Descriptions.Item label="指标口径">
            <div>
              <Text strong>{plan.metric_logic.metric_name}</Text>
              <div>
                <Text type="secondary">{plan.metric_logic.definition}</Text>
              </div>
            </div>
          </Descriptions.Item>
        )}
        {(plan.group_by || []).length > 0 && (
          <Descriptions.Item label="分组">{plan.group_by.join(", ")}</Descriptions.Item>
        )}
        <Descriptions.Item label="置信度">{plan.confidence}</Descriptions.Item>
      </Descriptions>
      <div style={{ marginTop: 8 }}>
        <Button
          size="small"
          icon={<ExclamationCircleOutlined />}
          danger
          onClick={() => setOpen(true)}
        >
          这个理解不对
        </Button>
      </div>
      <Modal
        title="反馈查询计划理解有误"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={sendFeedback}
        okText="提交反馈"
      >
        <Paragraph type="secondary">请简单说明哪里理解错了,便于修正口径或规则。</Paragraph>
        <Input.TextArea
          rows={3}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="例如:逾期率口径应该是逾期本金/贷款余额,不是借据数占比"
        />
      </Modal>
    </>
  );
}

// ---------------- 模块 7/8:SQL 预览卡片 ----------------

export function SqlCard({
  sql,
  traceId,
  node,
}: {
  sql: string;
  traceId: string;
  node: string;
}) {
  const [open, setOpen] = useState(false);
  const [comment, setComment] = useState("");

  const copy = () => navigator.clipboard.writeText(sql);

  const sendFeedback = async () => {
    await apiPost("/api/feedback", {
      trace_id: traceId,
      node,
      feedback_type: "sql_wrong",
      comment,
    });
    setOpen(false);
    setComment("");
  };

  return (
    <>
      <div className="preview-surface sql-surface">
        <div className="sql-surface-head">
          <span className="sql-surface-label">SQL</span>
          <Button type="text" size="small" icon={<CopyOutlined />} onClick={copy}>
            复制
          </Button>
        </div>
        <SqlHighlight sql={sql} />
      </div>
      <Space style={{ marginTop: 8 }}>
        <Button
          size="small"
          danger
          icon={<ExclamationCircleOutlined />}
          onClick={() => setOpen(true)}
        >
          这个不对
        </Button>
      </Space>
      <Modal
        title="反馈 SQL 有误"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={sendFeedback}
        okText="提交反馈"
      >
        <Input.TextArea
          rows={3}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="例如:缺少了产品编码过滤,应该只查 risk_mart 系统"
        />
      </Modal>
    </>
  );
}

// ---------------- 模块 10:结果表格 ----------------

export function ResultTable({ rows }: { rows: Record<string, any>[] }) {
  if (!rows || rows.length === 0) return <Text type="secondary">无结果</Text>;
  const columns = Object.keys(rows[0]).map((k) => ({
    title: k,
    dataIndex: k,
    key: k,
    ellipsis: true,
    render: (v: any) => String(v ?? ""),
  }));
  return (
    <Table
      size="small"
      columns={columns}
      dataSource={rows.map((r, i) => ({ ...r, key: i }))}
      pagination={{ pageSize: 20, showSizeChanger: false }}
      scroll={{ x: true }}
    />
  );
}

// ---------------- 重试过程时间线 ----------------

export function RetryTimeline({ retries }: { retries: { attempt: number; reason: string }[] }) {
  if (!retries || retries.length === 0) return null;
  return (
    <Collapse
      size="small"
      items={[
        {
          key: "retries",
          label: (
            <Space>
              <SyncOutlined />
              <Text>系统重试过 {retries.length} 次,查看处理过程</Text>
            </Space>
          ),
          children: (
            <Timeline
              items={retries.map((r, i) => ({
                color: i === retries.length - 1 ? "blue" : "gray",
                children: (
                  <div>
                    <Text strong>第 {r.attempt} 次重试</Text>
                    <div>
                      <Text type="secondary">{r.reason}</Text>
                    </div>
                  </div>
                ),
              }))}
            />
          ),
        },
      ]}
    />
  );
}

// ---------------- 结果摘要 ----------------

export function AnswerCard({
  answer,
  summary,
}: {
  answer?: string | null;
  summary?: ResultSummary | null;
}) {
  if (!answer && !summary) return null;
  if (!summary) {
    return (
      <Card size="small" bordered={false} style={{ background: "#eef7f1", borderRadius: 14 }}>
        <Paragraph style={{ fontSize: 15, lineHeight: 1.75, whiteSpace: "pre-line", marginBottom: 0 }}>
          {answer}
        </Paragraph>
      </Card>
    );
  }
  return (
    <Card
      size="small"
      bordered={false}
      style={{ background: "linear-gradient(145deg, #f4f8f4 0%, #edf5f0 100%)", borderRadius: 14 }}
      styles={{ body: { padding: "18px 20px" } }}
    >
      <Space direction="vertical" size={14} style={{ width: "100%" }}>
        <div>
          <Space size={8} align="start">
            <BulbOutlined style={{ color: "#5f806e", marginTop: 5 }} />
            <Text strong style={{ fontSize: 17, color: "#29483a" }}>{summary.headline}</Text>
          </Space>
          <Paragraph style={{ margin: "8px 0 0 26px", fontSize: 15, lineHeight: 1.75, color: "#496056" }}>
            {summary.overview}
          </Paragraph>
        </div>
        {summary.key_findings.length > 0 && (
          <div style={{ marginLeft: 26 }}>
            <Text strong>关键发现</Text>
            <List
              size="small"
              split={false}
              dataSource={summary.key_findings}
              renderItem={(item) => (
                <List.Item style={{ padding: "5px 0", color: "#3f5149" }}>
                  <span style={{ color: "#7d9a89", marginRight: 9 }}>●</span>{item}
                </List.Item>
              )}
            />
          </div>
        )}
        {summary.caveats.length > 0 && (
          <div style={{ marginLeft: 26, padding: "10px 12px", borderRadius: 10, background: "rgba(255,255,255,.58)" }}>
            <Space size={7} align="start">
              <InfoCircleOutlined style={{ color: "#9a7b52", marginTop: 4 }} />
              <div>
                {summary.caveats.map((item) => (
                  <Text key={item} type="secondary" style={{ display: "block", lineHeight: 1.65 }}>{item}</Text>
                ))}
              </div>
            </Space>
          </div>
        )}
      </Space>
    </Card>
  );
}

export { Divider };
