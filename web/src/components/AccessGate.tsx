import { useEffect, useState, type ReactNode } from "react";
import { Button, Card, Input, Spin, Typography, message } from "antd";
import { LockOutlined } from "@ant-design/icons";
import { apiPost, clearAccessToken, getAccessToken, setAccessToken } from "../api";

const { Text, Title } = Typography;

type GateState = "checking" | "open" | "locked";

export default function AccessGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GateState>("checking");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const check = async () => {
      try {
        const response = await fetch("/api/access/status");
        const status = await response.json() as { required: boolean };
        if (!status.required) {
          setState("open");
          return;
        }
        if (getAccessToken()) {
          try {
            await apiPost("/api/access/verify");
            setState("open");
            return;
          } catch {
            clearAccessToken();
          }
        }
        setState("locked");
      } catch {
        setState("locked");
        message.error("无法连接平台服务，请确认本机服务正在运行");
      }
    };
    void check();
  }, []);

  const login = async () => {
    const value = password.trim();
    if (!value) {
      message.warning("请输入平台访问密码");
      return;
    }
    setSubmitting(true);
    setAccessToken(value);
    try {
      await apiPost("/api/access/verify");
      setPassword("");
      setState("open");
    } catch (error) {
      clearAccessToken();
      message.error(error instanceof Error ? error.message : "访问密码不正确");
    } finally {
      setSubmitting(false);
    }
  };

  if (state === "checking") {
    return <div className="access-loading"><Spin size="large" /></div>;
  }
  if (state === "open") return <>{children}</>;

  return (
    <main className="access-page">
      <Card className="access-card">
        <div className="access-brand"><LockOutlined /></div>
        <Title level={2}>访问 NL2SQL 平台</Title>
        <Text type="secondary">该平台包含受保护的数据分析能力，请输入共享访问密码。</Text>
        <Input.Password
          size="large"
          prefix={<LockOutlined />}
          placeholder="平台访问密码"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          onPressEnter={login}
          autoFocus
        />
        <Button type="primary" size="large" block loading={submitting} onClick={login}>进入平台</Button>
        <Text type="secondary" className="access-hint">密码由平台管理员提供，请勿转发给无关人员。</Text>
      </Card>
    </main>
  );
}
