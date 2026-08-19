import { lazy, Suspense, useEffect, useState } from "react";
import { Button, Layout, Menu, Spin, Tooltip, Typography } from "antd";
import {
  BulbOutlined,
  ApartmentOutlined,
  CheckCircleOutlined,
  DatabaseOutlined,
  HistoryOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MessageOutlined,
  SettingOutlined,
  TableOutlined,
} from "@ant-design/icons";
import ErrorBoundary from "./components/ErrorBoundary";
import { APPROVAL_ENABLED } from "./config/features";

const QueryPage = lazy(() => import("./pages/QueryPage"));
const ApprovalsPage = lazy(() => import("./pages/ApprovalsPage"));
const HistoryPage = lazy(() => import("./pages/HistoryPage"));
const ConfigPage = lazy(() => import("./pages/ConfigPage"));
const SchemaPage = lazy(() => import("./pages/SchemaPage"));
const DatabasePage = lazy(() => import("./pages/DatabasePage"));
const RelationsPage = lazy(() => import("./pages/RelationsPage"));

const { Sider, Content } = Layout;
const { Text } = Typography;

type PageKey = "query" | "approvals" | "schema" | "databases" | "relations" | "history" | "config";

export default function App() {
  const [page, setPage] = useState<PageKey>("query");
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("app-nav-collapsed") === "true",
  );

  useEffect(() => {
    localStorage.setItem("app-nav-collapsed", String(collapsed));
  }, [collapsed]);

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
    { key: "history", icon: <HistoryOutlined />, label: "历史与审计" },
    { key: "config", icon: <SettingOutlined />, label: "配置管理" },
  ];

  return (
    <Layout className="app-shell">
      <Sider
        className="app-sider"
        width={224}
        collapsedWidth={72}
        collapsed={collapsed}
        trigger={null}
        theme="light"
        breakpoint="md"
        onBreakpoint={(broken) => broken && setCollapsed(true)}
      >
        <div className={`brand-lockup ${collapsed ? "collapsed" : ""}`}>
          <span className="brand-mark"><BulbOutlined /></span>
          {!collapsed && (
            <span className="brand-copy">
              <Text className="brand-title">NL2SQL 智能体</Text>
              <Text className="brand-subtitle">数据分析工作台</Text>
            </span>
          )}
        </div>

        <Menu
          mode="inline"
          selectedKeys={[page]}
          onClick={(event) => setPage(event.key as PageKey)}
          className="app-nav"
          inlineCollapsed={collapsed}
          defaultOpenKeys={["data-management"]}
          items={menuItems}
        />

        <div className="app-sider-footer">
          <Tooltip title={collapsed ? "展开功能菜单" : undefined} placement="right">
            <Button
              type="text"
              className="app-sider-toggle"
              icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              onClick={() => setCollapsed((value) => !value)}
              aria-label={collapsed ? "展开功能菜单" : "收起功能菜单"}
            >
              {!collapsed && "收起菜单"}
            </Button>
          </Tooltip>
        </div>
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
              {page === "config" && <ConfigPage />}
            </Suspense>
          </ErrorBoundary>
        </Content>
      </Layout>
    </Layout>
  );
}
