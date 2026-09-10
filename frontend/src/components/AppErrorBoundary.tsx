import React from "react";

type AppErrorBoundaryState = {
  error: Error | null;
  componentStack: string;
};

export class AppErrorBoundary extends React.Component<React.PropsWithChildren, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = { error: null, componentStack: "" };

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { error, componentStack: "" };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("Trip UI crashed", error, info);
    this.setState({ componentStack: info.componentStack ?? "" });
  }

  copyErrorDetails = async () => {
    const { error, componentStack } = this.state;
    const details = [
      `message: ${error?.message || "未知前端错误"}`,
      "",
      "stack:",
      error?.stack || "(empty)",
      "",
      "componentStack:",
      componentStack || "(empty)"
    ].join("\n");
    await navigator.clipboard?.writeText(details);
  };

  render() {
    if (!this.state.error) {
      return this.props.children;
    }

    return (
      <main className="app-error-boundary" role="alert">
        <section>
          <p>界面遇到异常，已阻止整页空白。</p>
          <small>{this.state.error.message || "未知前端错误"}</small>
          <details>
            <summary>错误详情</summary>
            <pre>{[
              this.state.error.stack,
              this.state.componentStack ? `Component stack:\n${this.state.componentStack}` : ""
            ].filter(Boolean).join("\n\n") || "暂无更多错误详情"}</pre>
          </details>
          <button type="button" onClick={this.copyErrorDetails}>
            复制错误详情
          </button>
          <button type="button" onClick={() => window.location.reload()}>
            刷新页面
          </button>
        </section>
      </main>
    );
  }
}
