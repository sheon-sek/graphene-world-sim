import { lazy, Suspense } from "react";
import { AppShell } from "./app/AppShell";
import { useRoute } from "./app/router";
import { SessionsPage } from "./app/SessionsPage";
import "./app/app.css";

// The 3D stack loads only where it is shown.
const HeroPage = lazy(() => import("./hero/HeroPage").then((m) => ({ default: m.HeroPage })));

export function App() {
  const route = useRoute();
  if (route.name === "hero")
    return (
      <Suspense fallback={<div className="hero-loading">Loading the hall…</div>}>
        <HeroPage />
      </Suspense>
    );
  if (route.name === "session") return <AppShell sid={route.session} workspace={route.workspace} />;
  return <SessionsPage />;
}
