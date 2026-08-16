import React from "react";

/** Per-page error boundary (Wave 6.1 / F-023): a throwing page renders an error
 *  panel INSIDE its own route; the nav shell and every other page stay alive.
 *  This is the structural replacement for the legacy single try/catch around the
 *  1,721-line renderVals() (support.js:928-933). */
export class PageBoundary extends React.Component<
  { page: string; children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error(`[${this.props.page}] page render failed:`, error);
  }

  render() {
    if (this.state.error) {
      return (
        <div role="alert" style={{ padding: 24 }}>
          <h2>The “{this.props.page}” page failed to render.</h2>
          <pre>{String(this.state.error)}</pre>
          <p>Other pages are unaffected. Reload to retry.</p>
        </div>
      );
    }
    return this.props.children;
  }
}
