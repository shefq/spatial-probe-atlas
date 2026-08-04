import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button, EmptyState } from "../components/ui";

export class GlobalErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Unhandled UI error", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="fatal-page">
        <EmptyState icon="!" title="This page could not be displayed" actions={
          <><Button variant="primary" onClick={() => this.setState({ error: null })}>Try again</Button><Button onClick={() => window.location.assign("/settings")}>Open diagnostics</Button></>
        }>
          Your project data was not changed. {this.state.error.message}
        </EmptyState>
      </main>
    );
  }
}
