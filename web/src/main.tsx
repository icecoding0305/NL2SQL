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
          colorPrimary: "#425f9d",
          colorInfo: "#425f9d",
          colorSuccess: "#4b806a",
          colorWarning: "#b9823b",
          colorError: "#b95f68",
          colorText: "#27303f",
          colorTextSecondary: "#687181",
          colorBgLayout: "#f4f5f3",
          colorBgContainer: "#ffffff",
          colorBorder: "#dcdfe3",
          colorBorderSecondary: "#e9ebed",
          borderRadius: 11,
          borderRadiusLG: 16,
          controlHeight: 38,
          fontSize: 14,
        },
        components: {
          Button: {
            borderRadius: 10,
            primaryShadow: "0 5px 14px rgba(66, 95, 157, 0.2)",
          },
          Card: { headerBg: "transparent" },
          Menu: {
            itemSelectedBg: "#edf2fa",
            itemSelectedColor: "#425f9d",
            itemHoverBg: "#f0f2f4",
          },
          Table: {
            headerBg: "#f7f8f7",
            rowHoverBg: "#f4f7fa",
          },
          Tabs: {
            itemSelectedColor: "#425f9d",
            inkBarColor: "#425f9d",
          },
        },
      }}
    >
      <AccessGate><App /></AccessGate>
    </ConfigProvider>
  </React.StrictMode>,
);
