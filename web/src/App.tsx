import { lazy, Suspense, useEffect, useState } from "react";
import { Layout, Menu, Spin, Typography } from "antd";
import {
  BulbOutlined,
  BookOutlined,
  ApartmentOutlined,
  AppstoreOutlined,
  CheckCircleOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  HistoryOutlined,
  SettingOutlined,
  MessageOutlined,
  SafetyCertificateOutlined,
  SwapOutlined,
  TableOutlined,
  TagsOutlined,
} from "@ant-design/icons";
import ErrorBoundary from "./components/ErrorBoundary";
import { APPROVAL_ENABLED } from "./config/features";

const QueryPage = lazy(() => import("./pages/QueryPage"));
const ApprovalsPage = lazy(() => import("./pages/ApprovalsPage"));
const HistoryPage = lazy(() => import("./pages/HistoryPage"));
const KnowledgePage = lazy(() => import("./pages/KnowledgePage"));
const SchemaPage = lazy(() => import("./pages/SchemaPage"));
const DatabasePage = lazy(() => import("./pages/DatabasePage"));
const RelationsPage = lazy(() => import("./pages/RelationsPage"));
const EvaluationPage = lazy(() => import("./pages/EvaluationPage"));

const { Sider, Content } = Layout;
const { Text } = Typography;

type PageKey = "query" | "approvals" | "schema" | "databases" | "relations" | "history"
  | "evaluation" | "knowledge-overview" | "knowledge-terms" | "knowledge-synonyms" | "knowledge-rules" | "knowledge-cases";

const MENU_PARENT_BY_PAGE: Partial<Record<PageKey, string>> = {
  databases: "data-management",
  schema: "data-management",
  relations: "data-management",
  "knowledge-overview": "knowledge-management",
  "knowledge-terms": "knowledge-management",
  "knowledge-synonyms": "knowledge-management",
  "knowledge-rules": "knowledge-management",
  "knowledge-cases": "knowledge-management",
  history: "system-management",
  evaluation: "system-management",
};

export default function App() {
  const [page, setPage] = useState<PageKey>("query");
  const [openMenuKeys, setOpenMenuKeys] = useState<string[]>([]);

  useEffect(() => {
    const parentKey = MENU_PARENT_BY_PAGE[page];
    if (parentKey) {
      setOpenMenuKeys((keys) => keys.includes(parentKey) ? keys : [...keys, parentKey]);
    }
  }, [page]);

  const menuItems = [
    { key: "query", icon: <MessageOutlined />, label: "数据问答" },
    ...(APPROVAL_ENABLED
      ? [{ key: "approvals", icon: <CheckCircleOutlined />, label: "审批队列" }]
      : []),
    {
      key: "data-management",
      icon: <DatabaseOutlined />,
      label: "数据源管理",
      children: [
        { key: "databases", icon: <DatabaseOutlined />, label: "数据库连接" },
        { key: "schema", icon: <TableOutlined />, label: "表与注释" },
        { key: "relations", icon: <ApartmentOutlined />, label: "表关系配置" },
      ],
    },
    {
      key: "knowledge-management",
      icon: <BookOutlined />,
      label: "企业知识管理",
      children: [
        { key: "knowledge-overview", icon: <AppstoreOutlined />, label: "知识概览" },
        { key: "knowledge-terms", icon: <TagsOutlined />, label: "业务名词" },
        { key: "knowledge-synonyms", icon: <SwapOutlined />, label: "同义表达" },
        { key: "knowledge-rules", icon: <SafetyCertificateOutlined />, label: "业务规则" },
        { key: "knowledge-cases", icon: <ExperimentOutlined />, label: "优化案例" },
      ],
    },
    {
      key: "system-management",
      icon: <SettingOutlined />,
      label: "系统管理",
      children: [
        { key: "history", icon: <HistoryOutlined />, label: "历史与审计" },
        { key: "evaluation", icon: <ExperimentOutlined />, label: "召回评测" },
      ],
    },
  ];

  return (
    <Layout className="app-shell">
      <Sider
        className="app-sider"
        width={224}
        trigger={null}
        theme="light"
      >
        <div className="brand-lockup">
          <span className="brand-mark"><BulbOutlined /></span>
          <span className="brand-copy">
            <Text className="brand-title">NL2SQL 智能体</Text>
            <Text className="brand-subtitle">数据分析工作台</Text>
          </span>
        </div>

        <Menu
          mode="inline"
          selectedKeys={[page]}
          onClick={(event) => setPage(event.key as PageKey)}
          className="app-nav"
          inlineIndent={18}
          openKeys={openMenuKeys}
          onOpenChange={(keys) => setOpenMenuKeys(keys.map(String))}
          items={menuItems}
        />
      </Sider>

      <Layout className="app-main-layout">
        <Content className="app-content">
          <ErrorBoundary>
            <Suspense fallback={<div style={{ height: "60vh", display: "grid", placeItems: "center" }}><Spin /></div>}>
              {page === "query" && <QueryPage />}
              {APPROVAL_ENABLED && page === "approvals" && <ApprovalsPage />}
              {page === "schema" && <SchemaPage />}
              {page === "databases" && <DatabasePage />}
              {page === "relations" && <RelationsPage />}
              {page === "history" && <HistoryPage />}
              {page === "evaluation" && <EvaluationPage />}
              {page === "knowledge-overview" && <KnowledgePage view="overview" />}
              {page === "knowledge-terms" && <KnowledgePage view="term" />}
              {page === "knowledge-synonyms" && <KnowledgePage view="synonym" />}
              {page === "knowledge-rules" && <KnowledgePage view="business_rule" />}
              {page === "knowledge-cases" && <KnowledgePage view="optimization_case" />}
            </Suspense>
          </ErrorBoundary>
        </Content>
      </Layout>
    </Layout>
  );
}
