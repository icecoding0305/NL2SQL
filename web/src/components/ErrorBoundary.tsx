import { Component, type ReactNode } from "react";
import { Alert } from "antd";

/** 错误边界:渲染出错时显示错误信息,而不是整页白屏。 */
export default class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <Alert
          type="error"
          showIcon
          message="页面渲染出错"
          description={
            <div>
              <div>{String(this.state.error)}</div>
              <pre style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>
                {this.state.error.stack}
              </pre>
            </div>
          }
          style={{ margin: 16 }}
        />
      );
    }
    return this.props.children;
  }
}
