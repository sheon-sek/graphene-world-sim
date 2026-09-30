import { Component, type ReactNode } from "react";
import { fallBackToWebGL } from "./backend";

/** Catches a failure inside the 3D view: retries on WebGL2, or shows why it stopped. */
export class SceneBoundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };

  static getDerivedStateFromError(error: unknown) {
    return { error: String(error) };
  }

  componentDidCatch(error: unknown) {
    fallBackToWebGL(error);
  }

  render() {
    if (this.state.error) return <div className="hero-error">The 3D view stopped: {this.state.error}</div>;
    return this.props.children;
  }
}
