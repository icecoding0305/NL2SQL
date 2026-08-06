import { lazy, Suspense, useState } from "react";
import { Layout, Menu, Spin, Typography } from "antd";
import {
  CheckCircleOutlined,
  DatabaseOutlined,
  HistoryOutlined,
  SettingOutlined,
  TableOutlined,
  BulbOutlined,
} from "@ant-design/icons";
import ErrorBoundary from "./components/ErrorBoundary";
const QueryPage = lazy(() => import("./pages/QueryPage"));
const ApprovalsPage = lazy(() => import("./pages/ApprovalsPage"));
const HistoryPage = lazy(() => import("./pages/HistoryPage"));
const ConfigPage = lazy(() => import("./pages/ConfigPage"));
const SchemaPage = lazy(() => import("./pages/SchemaPage"));

const { Header, Content } = Layout;
const { Text } = Typography;

type PageKey = "query" | "approvals" | "schema" | "history" | "config";

export default function App() {
  const [page, setPage] = useState<PageKey>("query");

  return (
    <Layout className="app-shell">
      <Header className="app-header">
        <div className="brand-lockup">
          <span className="brand-mark"><BulbOutlined /></span>
          <Text className="brand-title">NL2SQL 智能体</Text>
        </div>
        <Menu
          mode="horizontal"
          selectedKeys={[page]}
          onClick={(e) => setPage(e.key as PageKey)}
          className="app-nav"
          items={[
            { key: "query", icon: <DatabaseOutlined />, label: "数据问答" },
            { key: "approvals", icon: <CheckCircleOutlined />, label: "审批队列" },
            { key: "schema", icon: <TableOutlined />, label: "表与注释" },
            { key: "history", icon: <HistoryOutlined />, label: "历史与审计" },
            { key: "config", icon: <SettingOutlined />, label: "配置管理" },
          ]}
        />
      </Header>
      <Content style={{ overflowY: "auto", background: "#f7f8fa" }}>
        <ErrorBoundary>
          <Suspense fallback={<div style={{ height: "60vh", display: "grid", placeItems: "center" }}><Spin /></div>}>
            {page === "query" && <QueryPage />}
            {page === "approvals" && <ApprovalsPage />}
            {page === "schema" && <SchemaPage />}
            {page === "history" && <HistoryPage />}
            {page === "config" && <ConfigPage />}
          </Suspense>
        </ErrorBoundary>
      </Content>
    </Layout>
  );
}
