import { useEffect, useMemo, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import { ReconnectingSocket } from "../api/streams";
import type { JobSnapshot, ResourceWarning } from "../api/types";
import { ResourceStatus } from "../components/ResourceStatus";
import { Button, InlineAlert, Skeleton, StatusBadge } from "../components/ui";
import { useCameraStore, useDiagnosticsStore, useJobStore, useProjectStore, useUiStore } from "../stores";
import { formatBytes } from "../utils/format";
import { GlobalErrorBoundary } from "./ErrorBoundary";

export function RootLayout() {
  const capabilities = useDiagnosticsStore((state) => state.capabilities);
  const setCapabilities = useDiagnosticsStore((state) => state.setCapabilities);
  const setResources = useDiagnosticsStore((state) => state.setResources);
  const cameraState = useCameraStore((state) => state.status.state);
  const upsertJob = useJobStore((state) => state.upsert);
  const pushToast = useUiStore((state) => state.pushToast);

  useEffect(() => {
    const controller = new AbortController();
    const refresh = () => {
      void api.system.capabilities(controller.signal).then(setCapabilities).catch(() => undefined);
      void api.system.resources(controller.signal).then(setResources).catch(() => undefined);
    };
    refresh();
    const timer = window.setInterval(refresh, 5000);
    const events = new ReconnectingSocket("/ws/v1/events", {
      onEnvelope: (envelope) => {
        if (envelope.type.startsWith("job.")) upsertJob(envelope.data as JobSnapshot);
        if (envelope.type === "resource.warning") {
          const warning = envelope.data as ResourceWarning;
          pushToast({ kind: warning.severity === "critical" ? "error" : "warning", title: warning.message, message: warning.suggested_action });
        }
      },
    });
    events.connect();
    return () => { controller.abort(); window.clearInterval(timer); events.close(); };
  }, [pushToast, setCapabilities, setResources, upsertJob]);

  return (
    <div className="app-shell">
      <header className="app-topbar">
        <Link className="brand" to="/projects"><span className="brand__mark">SPA</span><span><strong>Spatial Probe Atlas</strong><small>LOCAL WORKSPACE</small></span></Link>
        <div className="app-topbar__status">
          <StatusBadge state={cameraState} label={`Camera · ${cameraState.replaceAll("_", " ")}`} />
          <StatusBadge state={capabilities?.compute_state} label={capabilities?.effective_compute_profile ?? "Compute unknown"} />
          <ResourceStatus compact />
        </div>
        <NavLink to="/settings" className={({ isActive }) => `icon-link ${isActive ? "is-active" : ""}`} aria-label="Settings and diagnostics">⚙</NavLink>
      </header>
      <GlobalErrorBoundary><Outlet /></GlobalErrorBoundary>
      <ToastViewport />
    </div>
  );
}

const steps = [
  { route: "camera", label: "Camera", key: "camera_ready" },
  { route: "mapping", label: "Mapping", key: "map_ready" },
  { route: "registration", label: "Probe & Registration", key: "registration_ready" },
  { route: "live", label: "Live Painting", key: "live_ready" },
] as const;

export function ProjectLayout() {
  const { projectId = "" } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const activeProject = useProjectStore((state) => state.activeProject);
  const setActiveProject = useProjectStore((state) => state.setActiveProject);
  const setActiveMap = useProjectStore((state) => state.setActiveMap);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    api.projects.summary(projectId, controller.signal)
      .then((summary) => {
        setActiveProject(summary);
        if (summary.active_map_id) {
          return api.maps.list(projectId, controller.signal).then((maps) => setActiveMap(maps.find((map) => map.id === summary.active_map_id) ?? null));
        }
        setActiveMap(null);
      })
      .catch((value) => { if (!controller.signal.aborted) setError(errorMessage(value)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [projectId, setActiveMap, setActiveProject]);

  const readiness = activeProject?.readiness;
  const pathStep = steps.findIndex((step) => location.pathname.includes(`/${step.route}`));
  const warnings = activeProject?.warnings ?? [];

  if (loading) return <main className="project-loading"><Skeleton lines={6} /></main>;
  if (error || !activeProject) {
    return <main className="project-loading"><InlineAlert tone="danger" title="Project unavailable" action={<Button onClick={() => navigate("/projects")}>Back to projects</Button>}>{error ?? "The project was not found."}</InlineAlert></main>;
  }

  return (
    <div className="project-shell">
      <header className="project-header">
        <div className="project-header__identity">
          <Link to="/projects" className="back-link">← Projects</Link>
          <h1>{activeProject.name}</h1>
          <StatusBadge state={activeProject.state} />
        </div>
        <div className="project-header__metrics">
          <span><small>Project size</small>{formatBytes(activeProject.size_bytes)}</span>
          <span><small>Map points</small>{activeProject.map_point_count?.toLocaleString() ?? "—"}</span>
          <span><small>Sessions</small>{activeProject.session_count ?? 0}</span>
        </div>
      </header>
      <nav className="workflow-stepper" aria-label="Project workflow">
        {steps.map((step, index) => {
          const complete = step.key === "live_ready"
            ? Boolean(readiness?.camera_ready && readiness?.map_ready && readiness?.probe_calibration_ready && readiness?.registration_ready)
            : Boolean(readiness?.[step.key as keyof typeof readiness]);
          return (
            <NavLink key={step.route} to={`/projects/${projectId}/${step.route}`} className={({ isActive }) => `workflow-step ${isActive ? "is-active" : ""} ${complete ? "is-complete" : ""}`}>
              <span>{complete ? "✓" : index + 1}</span><span>{step.label}</span>
            </NavLink>
          );
        })}
        {activeProject.sessions?.length ? (
          <NavLink to={`/projects/${projectId}/sessions/${activeProject.sessions[0].id}/review`} className={({ isActive }) => `workflow-step ${isActive ? "is-active" : ""}`}>
            <span>5</span><span>Review</span>
          </NavLink>
        ) : <span className="workflow-step is-disabled"><span>5</span><span>Review</span></span>}
      </nav>
      {warnings.length ? <div className="project-warning-strip">{warnings.map((warning, index) => <span key={warning.id ?? index}>△ {warning.message}</span>)}</div> : null}
      <main className="project-content" data-workflow-step={pathStep}><Outlet /></main>
    </div>
  );
}

export function RequirementGate({ requirements, children }: { requirements: Array<{ ready: boolean | undefined; label: string; route: string }>; children: React.ReactNode }) {
  const missing = requirements.filter((requirement) => !requirement.ready);
  if (!missing.length) return children;
  return (
    <div className="requirement-gate">
      <InlineAlert tone="warning" title="Complete the required setup">
        {missing.map((item) => <Link key={item.route} to={item.route}>{item.label} →</Link>)}
      </InlineAlert>
      {children}
    </div>
  );
}

function ToastViewport() {
  const toasts = useUiStore((state) => state.toasts);
  const dismiss = useUiStore((state) => state.dismissToast);
  useEffect(() => {
    if (!toasts.length) return;
    const timer = window.setTimeout(() => dismiss(toasts[0].id), 5000);
    return () => window.clearTimeout(timer);
  }, [dismiss, toasts]);
  return (
    <div className="toast-viewport" aria-live="polite">
      {toasts.map((toast) => <div className={`toast toast--${toast.kind}`} key={toast.id}><div><strong>{toast.title}</strong>{toast.message ? <p>{toast.message}</p> : null}</div><button onClick={() => dismiss(toast.id)} aria-label="Dismiss">×</button></div>)}
    </div>
  );
}
