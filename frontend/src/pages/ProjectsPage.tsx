import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, errorMessage, unwrapList } from "../api/client";
import type { JobSnapshot, Project, ProjectSummary } from "../api/types";
import { ResourceStatus } from "../components/ResourceStatus";
import { Button, Card, EmptyState, Field, InlineAlert, Metric, Modal, Skeleton, StatusBadge, TextInput, Toggle } from "../components/ui";
import { useDiagnosticsStore, useProjectStore, useUiStore } from "../stores";
import { formatBytes, formatCount, formatDate, formatDuration, validateProjectName } from "../utils/format";

export function ProjectsPage() {
  const navigate = useNavigate();
  const projects = useProjectStore((state) => state.projects);
  const setProjects = useProjectStore((state) => state.setProjects);
  const loading = useProjectStore((state) => state.loading);
  const setLoading = useProjectStore((state) => state.setLoading);
  const compute = useDiagnosticsStore((state) => state.capabilities);
  const pushToast = useUiStore((state) => state.pushToast);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selected, setSelected] = useState<ProjectSummary | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [legacyOpen, setLegacyOpen] = useState(false);
  const [legacySource, setLegacySource] = useState("");
  const [legacyName, setLegacyName] = useState("");
  const [confirmProbeDefaults, setConfirmProbeDefaults] = useState(false);
  const [legacyJob, setLegacyJob] = useState<(JobSnapshot & { target_project_id?: string }) | null>(null);
  const [legacyError, setLegacyError] = useState<string | null>(null);
  const [legacyBusy, setLegacyBusy] = useState(false);

  const refresh = (signal?: AbortSignal) => {
    setLoading(true);
    setListError(null);
    return api.projects.list(includeArchived, signal)
      .then((value) => setProjects(unwrapList(value)))
      .catch((error) => { if (!signal?.aborted) setListError(errorMessage(error)); })
      .finally(() => { if (!signal?.aborted) setLoading(false); });
  };

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [includeArchived]);

  const legacyActive = Boolean(legacyJob && ["queued", "admitted", "processing", "cancelling"].includes(legacyJob.state));
  useEffect(() => {
    if (!legacyJob?.id || !legacyActive) return;
    let stopped = false;
    const poll = async () => {
      try {
        const current = await api.projects.legacyImport(legacyJob.id);
        if (stopped) return;
        setLegacyJob(current);
        if (current.state === "completed") {
          await refresh();
          pushToast({ kind: "success", title: "Legacy project imported", message: `${current.result?.project_name ?? "Imported project"} is ready. The source directory was not changed.` });
        }
      } catch (value) {
        if (!stopped) setLegacyError(errorMessage(value));
      }
    };
    const timer = window.setInterval(() => void poll(), 600);
    void poll();
    return () => { stopped = true; window.clearInterval(timer); };
  }, [legacyJob?.id, legacyActive]);

  const queueLegacyImport = async () => {
    setLegacyBusy(true); setLegacyError(null);
    try {
      const job = await api.projects.importLegacy(legacySource.trim(), legacyName.trim() || undefined, confirmProbeDefaults);
      setLegacyJob(job);
      pushToast({ kind: "success", title: "Legacy import queued", message: "The selected source will be copied into same-volume staging and left untouched." });
    } catch (value) { setLegacyError(errorMessage(value)); }
    finally { setLegacyBusy(false); }
  };

  const createProject = async () => {
    const error = validateProjectName(name);
    if (error) return;
    setCreating(true);
    try {
      const project = await api.projects.create(name.trim());
      setNewOpen(false);
      setName("");
      pushToast({ kind: "success", title: "Project created", message: project.name });
      navigate(`/projects/${project.id}/camera`);
    } catch (errorValue) {
      pushToast({ kind: "error", title: "Project was not created", message: errorMessage(errorValue) });
    } finally {
      setCreating(false);
    }
  };

  const selectProject = async (project: Project) => {
    try { setSelected(await api.projects.summary(project.id)); }
    catch (error) { pushToast({ kind: "error", title: "Could not load project", message: errorMessage(error) }); }
  };

  return (
    <main className="page page--projects">
      <header className="page-heading page-heading--wide">
        <div className="page-heading__actions"><Button onClick={() => setLegacyOpen(true)}>Import legacy project</Button></div>
        <div><div className="eyebrow">LOCAL WORKSPACE</div><h1>Projects & Sessions</h1><p>Create a spatial reference, resume acquisition, or inspect a completed session.</p></div>
        <div className="page-heading__actions"><Button variant="primary" onClick={() => setNewOpen(true)}>＋ New project</Button></div>
      </header>
      <div className="projects-summary">
        <Card title="Workspace health" eyebrow="THIS MACHINE"><ResourceStatus /></Card>
        <Card title="Runtime" eyebrow="EFFECTIVE PROFILE">
          <div className="metric-grid">
            <Metric label="Compute" value={compute?.effective_compute_profile ?? "Detecting…"} />
            <Metric label="Record3D" value={compute?.record3d_state ?? "Checking…"} />
            <Metric label="Replay" value={compute?.replay_available === false ? "Unavailable" : "Ready"} />
          </div>
        </Card>
      </div>
      <section className="projects-list">
        <div className="section-toolbar">
          <div><h2>Workspace projects</h2><span className="muted">{projects.length} shown</span></div>
          <Toggle label="Show archived" checked={includeArchived} onChange={(event) => setIncludeArchived(event.target.checked)} />
        </div>
        {listError ? <InlineAlert tone="danger" title="Projects could not be loaded" action={<Button size="sm" onClick={() => void refresh()}>Retry</Button>}>{listError} Existing data remains untouched. Open Diagnostics if this continues.</InlineAlert> : null}
        {loading ? <div className="project-card-grid"><Card><Skeleton lines={5} /></Card><Card><Skeleton lines={5} /></Card></div> : null}
        {!loading && !listError && !projects.length ? (
          <EmptyState icon="⌖" title="Build your first spatial atlas" actions={<Button variant="primary" onClick={() => setNewOpen(true)}>Create first project</Button>}>
            Connect Record3D or use the included replay fixture, capture a scene, then register and track your five-marker probe. Data stays in your local workspace.
          </EmptyState>
        ) : null}
        {!loading && projects.length ? (
          <div className="project-card-grid">
            {projects.map((project) => (
              <article className={`project-card ${project.state === "archived" ? "is-archived" : ""}`} key={project.id} onClick={() => void selectProject(project)}>
                <div className="project-card__top"><span className="project-glyph">⌖</span><StatusBadge state={project.state} /></div>
                <div><h3>{project.name}</h3><p>Updated {formatDate(project.updated_at)}</p></div>
                <div className="project-card__readiness">
                  <span className={project.readiness?.camera_ready ? "is-ready" : ""}>Camera</span>
                  <span className={project.readiness?.map_ready ? "is-ready" : ""}>Map</span>
                  <span className={project.readiness?.probe_calibration_ready ? "is-ready" : ""}>Probe</span>
                  <span className={project.readiness?.registration_ready ? "is-ready" : ""}>Metric</span>
                </div>
                <div className="project-card__stats"><span><small>Map points</small>{formatCount(project.map_point_count)}</span><span><small>Sessions</small>{project.session_count ?? 0}</span><span><small>Size</small>{formatBytes(project.size_bytes)}</span></div>
                <div className="button-row"><Button variant="primary" size="sm" onClick={(event) => { event.stopPropagation(); navigate(`/projects/${project.id}/${project.readiness?.map_ready ? "registration" : "camera"}`); }}>{project.state === "archived" ? "Inspect" : "Open project"}</Button><Button size="sm" onClick={(event) => { event.stopPropagation(); void selectProject(project); }}>Details</Button></div>
              </article>
            ))}
          </div>
        ) : null}
      </section>
      <ProjectDrawer project={selected} onClose={() => setSelected(null)} onChanged={() => void refresh()} />
      <Modal open={newOpen} title="Create project" description="A project keeps map, calibration, registration and session revisions together." onRequestClose={() => { if (!creating) setNewOpen(false); }} size="sm" footer={<><Button onClick={() => setNewOpen(false)}>Cancel</Button><Button variant="primary" busy={creating} disabled={Boolean(validateProjectName(name))} onClick={() => void createProject()}>Create & set up camera</Button></>}>
        <Field label="Project name" error={name ? validateProjectName(name) : undefined} hint="1–80 characters. Windows reserved names are not allowed."><TextInput value={name} autoFocus maxLength={80} onChange={(event) => setName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void createProject(); }} placeholder="e.g. Phantom trial 08" /></Field>
      </Modal>
      <Modal open={legacyOpen} title="Import prototype project" description="Choose one existing prototype project directory. Spatial Probe Atlas copies it into same-volume staging, verifies every checksum, and never modifies the source." onRequestClose={() => setLegacyOpen(false)} size="md" footer={
        legacyJob?.state === "completed" ? <><Button onClick={() => api.projects.downloadLegacyReport(legacyJob.id)}>Download report</Button><Button variant="primary" onClick={() => navigate(`/projects/${legacyJob.result?.project_id ?? legacyJob.target_project_id}/camera`)}>Open imported project</Button></>
          : legacyActive ? <><Button onClick={() => setLegacyOpen(false)}>Run in background</Button><Button variant="danger" onClick={() => void api.jobs.cancel(legacyJob!.id)}>Cancel import</Button></>
            : <><Button onClick={() => setLegacyOpen(false)}>Cancel</Button><Button variant="primary" busy={legacyBusy} disabled={!legacySource.trim() || Boolean(legacyName.trim() && validateProjectName(legacyName))} onClick={() => void queueLegacyImport()}>Copy, validate & import</Button></>
      }>
        <div className="form-stack">
          <InlineAlert tone="info" title="Explicit directory only">Paste the absolute path of the legacy project you selected. The app does not scan this machine for prototype data.</InlineAlert>
          <Field label="Legacy project directory" hint="Absolute local path, for example D:\\LabData\\Prototype_Project"><TextInput value={legacySource} autoFocus disabled={legacyActive} onChange={(event) => setLegacySource(event.target.value)} placeholder="D:\\LabData\\Prototype_Project" /></Field>
          <Field label="New project name (optional)" error={legacyName.trim() ? validateProjectName(legacyName) : undefined} hint="If blank, the importer uses the legacy config or directory name."><TextInput value={legacyName} maxLength={80} disabled={legacyActive} onChange={(event) => setLegacyName(event.target.value)} /></Field>
          <Toggle label="I confirm v1 defaults may complete missing probe tip, quality, or blob-detector fields" checked={confirmProbeDefaults} disabled={legacyActive} onChange={(event) => setConfirmProbeDefaults(event.target.checked)} />
          <p className="muted">Defaults are used only when the recognized legacy probe calibration omits required v1 fields. Every defaulted field is listed in the migration report; imported registration metadata still requires repeat validation.</p>
          {legacyJob ? <Card title="Durable import job" eyebrow={legacyJob.state.toUpperCase()}><div className="metric-grid"><Metric label="Stage" value={legacyJob.stage ?? "Queued"} /><Metric label="Progress" value={`${Math.round((legacyJob.progress ?? 0) * 100)}%`} /></div><p className="muted">{legacyJob.message}</p>{legacyJob.result?.report_summary ? <p>{legacyJob.result.report_summary.recognized_file_count} recognized files · {legacyJob.result.report_summary.unknown_file_count} preserved under legacy_unmapped · {legacyJob.result.report_summary.warnings} warnings</p> : null}</Card> : null}
          {legacyJob?.error ? <InlineAlert tone="danger" title={legacyJob.error.code}>{legacyJob.error.message ?? "The import did not complete."}{legacyJob.error.details?.defaulted_fields?.length ? ` Review and confirm defaults for: ${legacyJob.error.details.defaulted_fields.join(", ")}.` : ""} {legacyJob.error.suggested_action}</InlineAlert> : null}
          {legacyError ? <InlineAlert tone="danger" title="Legacy import request failed">{legacyError}</InlineAlert> : null}
        </div>
      </Modal>
    </main>
  );
}

function ProjectDrawer({ project, onClose, onChanged }: { project: ProjectSummary | null; onClose: () => void; onChanged: () => void }) {
  const navigate = useNavigate();
  const pushToast = useUiStore((state) => state.pushToast);
  const [busy, setBusy] = useState(false);
  if (!project) return null;
  const run = async (action: "clone" | "archive" | "restore" | "reveal") => {
    setBusy(true);
    try {
      if (action === "clone") await api.projects.clone(project.id, `${project.name} copy`);
      else if (action === "archive") await api.projects.archive(project.id);
      else if (action === "restore") await api.projects.restore(project.id);
      else await api.projects.reveal(project.id);
      pushToast({ kind: "success", title: action === "reveal" ? "Project directory opened" : `Project ${action}d` });
      if (action !== "reveal") { onClose(); onChanged(); }
    } catch (error) { pushToast({ kind: "error", title: "Action failed", message: errorMessage(error) }); }
    finally { setBusy(false); }
  };
  return (
    <div className="drawer-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
      <aside className="drawer" aria-label={`${project.name} details`}>
        <header><div><div className="eyebrow">PROJECT DETAILS</div><h2>{project.name}</h2></div><Button variant="ghost" onClick={onClose} aria-label="Close details">×</Button></header>
        <div className="drawer__content">
          <div className="metric-grid"><Metric label="Size" value={formatBytes(project.size_bytes)} /><Metric label="Frames" value={formatCount(project.capture_frame_count)} /><Metric label="Map points" value={formatCount(project.map_point_count)} /><Metric label="Sessions" value={project.session_count ?? 0} /></div>
          <section><h3>Sessions</h3>{project.sessions?.length ? <div className="drawer-list">{project.sessions.map((session) => <button key={session.id} onClick={() => navigate(`/projects/${project.id}/sessions/${session.id}/review`)}><span><strong>{session.name}</strong><small>{formatDate(session.created_at)}</small></span><span>{formatDuration(session.duration_seconds)} →</span></button>)}</div> : <p className="muted">No sessions yet.</p>}</section>
          <section><h3>Recent processing</h3>{project.jobs?.length ? <div className="drawer-list">{project.jobs.map((job) => <div className="drawer-job" key={job.id}><span><strong>{job.type}</strong><small>{job.stage ?? job.message}</small></span><StatusBadge state={job.state} /></div>)}</div> : <p className="muted">No jobs recorded.</p>}</section>
        </div>
        <footer><Button variant="primary" disabled={project.state === "archived"} onClick={() => navigate(`/projects/${project.id}/camera`)}>Open project</Button><Button busy={busy} onClick={() => void run("clone")}>Clone</Button><Button busy={busy} onClick={() => void run("reveal")}>Reveal directory</Button><Button variant={project.state === "archived" ? "default" : "danger"} busy={busy} onClick={() => void run(project.state === "archived" ? "restore" : "archive")}>{project.state === "archived" ? "Restore" : "Archive"}</Button></footer>
      </aside>
    </div>
  );
}
