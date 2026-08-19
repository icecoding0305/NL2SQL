import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import "antd/dist/reset.css";
import "./app.css";
import App from "./App";
import AccessGate from "./components/AccessGate";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#2563eb",
          colorSuccess: "#16a34a",
          colorWarning: "#f59e0b",
          colorError: "#ef4444",
          colorText: "#1f2329",
          colorBgLayout: "#f7f8fa",
          colorBorder: "#e6e8ec",
          borderRadius: 10,
          fontSize: 14,
        },
      }}
    >
      <AccessGate><App /></AccessGate>
    </ConfigProvider>
  </React.StrictMode>,
);
