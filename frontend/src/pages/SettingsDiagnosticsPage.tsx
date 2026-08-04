import { useEffect, useMemo, useState } from "react";
import { api, errorMessage } from "../api/client";
import { diagnosticWorkflowApi, type LogLine } from "../api/diagnosticWorkflows";
import type { AppSettings, DiagnosticCheck } from "../api/types";
import { ResourceStatus } from "../components/ResourceStatus";
import { Button, Card, Field, InlineAlert, Metric, Modal, Skeleton, StatusBadge, TextInput, Toggle } from "../components/ui";
import { useDiagnosticsStore, useUiStore } from "../stores";
import { formatBytes, formatDate } from "../utils/format";

const defaultSettings: AppSettings = {
  display_units: "mm",
  compute_profile: "auto",
  point_budget: 3_000_000,
  decoded_cache_mib: 512,
  continue_live_in_background: false,
  log_level: "INFO",
};

export function SettingsDiagnosticsPage() {
  const capabilities = useDiagnosticsStore((state) => state.capabilities);
  const resources = useDiagnosticsStore((state) => state.resources);
  const checks = useDiagnosticsStore((state) => state.checks);
  const settings = useDiagnosticsStore((state) => state.settings);
  const setCapabilities = useDiagnosticsStore((state) => state.setCapabilities);
  const setResources = useDiagnosticsStore((state) => state.setResources);
  const setChecks = useDiagnosticsStore((state) => state.setChecks);
  const setSettings = useDiagnosticsStore((state) => state.setSettings);
  const setDisplayUnits = useUiStore((state) => state.setDisplayUnits);
  const pushToast = useUiStore((state) => state.pushToast);
  const [draft, setDraft] = useState<AppSettings>(settings ?? defaultSettings);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [migrateOpen, setMigrateOpen] = useState(false);
  const [dataRoot, setDataRoot] = useState("");

  const refresh = async (signal?: AbortSignal) => {
    setLoading(true); setError(null);
    const results = await Promise.allSettled([
      api.system.capabilities(signal).then(setCapabilities),
      api.system.resources(signal).then(setResources),
      api.system.settings().then((value) => { setSettings(value); setDraft(value); setDisplayUnits(value.display_units); }),
      diagnosticWorkflowApi.logTail().then((value) => setLogs(value.items)),
    ]);
    const failures = results.filter((result) => result.status === "rejected");
    if (failures.length) setError(`${failures.length} diagnostic component${failures.length === 1 ? "" : "s"} could not be refreshed. Other results remain available.`);
    setLoading(false);
  };
  useEffect(() => { const controller = new AbortController(); void refresh(controller.signal); return () => controller.abort(); }, []);

  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(settings), [draft, settings]);
  const validation = useMemo(() => ({
    point_budget: draft.point_budget < 500_000 || draft.point_budget > 10_000_000 ? "Use 0.5–10 million points." : null,
    decoded_cache_mib: draft.decoded_cache_mib < 128 || draft.decoded_cache_mib > 4096 ? "Use 128–4,096 MiB." : null,
  }), [draft]);
  const valid = !validation.point_budget && !validation.decoded_cache_mib;

  const runDiagnostics = async () => {
    setRunning(true); setError(null);
    try {
      const result = await api.system.diagnostics();
      if (result.checks) setChecks(result.checks);
      pushToast({ kind: "success", title: result.job_id ? "Diagnostics started" : "Diagnostics complete", message: result.job_id ? "Hardware and replay checks continue in the background." : undefined });
    } catch (value) { setError(errorMessage(value)); }
    finally { setRunning(false); }
  };
  const save = async () => {
    if (!valid) return;
    setSaving(true);
    try { const value = await api.system.updateSettings(draft); setSettings(value); setDraft(value); setDisplayUnits(value.display_units); pushToast({ kind: "success", title: "Settings saved", message: draft.compute_profile !== settings?.compute_profile ? "Compute profile applies to new jobs." : undefined }); }
    catch (value) { setError(errorMessage(value)); }
    finally { setSaving(false); }
  };
  const supportBundle = async () => {
    setRunning(true);
    try { await api.system.supportBundle(); pushToast({ kind: "success", title: "Support bundle job started", message: "Raw frames and imported file contents are excluded." }); }
    catch (value) { setError(errorMessage(value)); }
    finally { setRunning(false); }
  };
  const migrate = async () => {
    if (!dataRoot.trim()) return;
    setRunning(true);
    try { await diagnosticWorkflowApi.migrateDataRoot(dataRoot.trim()); setMigrateOpen(false); setDataRoot(""); pushToast({ kind: "success", title: "Data-root migration queued", message: "The source remains until checksums and database integrity pass. Restart will be required." }); }
    catch (value) { setError(errorMessage(value)); }
    finally { setRunning(false); }
  };

  return (
    <main className="page page--settings">
      <header className="page-heading page-heading--wide"><div><div className="eyebrow">LOCAL SYSTEM</div><h1>Settings & Diagnostics</h1><p>Inspect operational health, compute capability and local storage without changing project data.</p></div><div className="page-heading__actions"><Button busy={loading} onClick={() => void refresh()}>Refresh</Button><Button variant="primary" busy={running} onClick={() => void runDiagnostics()}>Run diagnostics</Button></div></header>
      {error ? <InlineAlert tone="warning" title="Some checks need attention" action={<Button size="sm" onClick={() => setError(null)}>Dismiss</Button>}>{error}</InlineAlert> : null}
      {loading && !capabilities ? <Skeleton lines={10} /> : (
        <>
          <div className="status-card-row diagnostics-overview"><Metric label="Application" value={capabilities?.app_version ?? "—"} detail={`API ${capabilities?.api_version ?? "—"}`} /><Metric label="Compute state" value={capabilities?.compute_state?.replaceAll("_", " ") ?? "—"} tone={capabilities?.compute_state === "degraded" || capabilities?.compute_state === "cuda_incompatible" ? "warning" : "good"} /><Metric label="Effective profile" value={capabilities?.effective_compute_profile ?? "—"} /><Metric label="Record3D" value={capabilities?.record3d_state ?? "—"} /><Metric label="Schema" value={capabilities?.schema_version ?? "—"} /></div>
          <div className="settings-grid">
            <div>
              <Card title="System resources" eyebrow="LIVE SNAPSHOT"><ResourceStatus /></Card>
              <Card title="Capability details" eyebrow="STARTUP PROBES">
                <div className="key-value-list"><div><span>CPU</span><strong>{capabilities?.cpu ?? "—"}</strong></div><div><span>GPU</span><strong>{capabilities?.gpu ?? "Not available — CPU is supported"}</strong></div><div><span>CUDA</span><strong>{capabilities?.cuda_version ?? "Not active"}</strong></div><div><span>Data root</span><code>{capabilities?.data_root ?? "—"}</code></div><div><span>Replay adapter</span><StatusBadge state={capabilities?.replay_available !== false} label={capabilities?.replay_available === false ? "not available" : "ready"} /></div></div>
              </Card>
              <Card title="Diagnostic checks" eyebrow={`${checks.length} RESULTS`} actions={<Button size="sm" busy={running} onClick={() => void runDiagnostics()}>Run again</Button>}>
                {checks.length ? <div className="diagnostic-checks">{checks.map((check) => <DiagnosticRow key={check.key} check={check} />)}</div> : <p className="muted">Run diagnostics for dependency, storage, database, replay and optional hardware checks. Unavailable hardware never blocks CPU/replay operation.</p>}
              </Card>
            </div>
            <aside>
              <Card title="Application settings" eyebrow="SAFE BOUNDS">
                <Field label="Display units"><select className="select" value={draft.display_units} onChange={(event) => setDraft((current) => ({ ...current, display_units: event.target.value as AppSettings["display_units"] }))}><option value="mm">Millimetres</option><option value="m">Metres</option></select></Field>
                <Field label="Compute profile" hint={draft.compute_profile === "cuda" ? "Only available after backend CUDA verification." : "Auto uses CUDA only after all startup checks pass."}><select className="select" value={draft.compute_profile} onChange={(event) => setDraft((current) => ({ ...current, compute_profile: event.target.value as AppSettings["compute_profile"] }))}><option value="auto">Auto (recommended)</option><option value="cpu">CPU only</option><option value="cuda" disabled={capabilities?.compute_state !== "cuda_ready"}>CUDA</option></select></Field>
                <Field label="Viewer point budget" error={validation.point_budget ?? undefined} hint="Visible points; resource pressure can reduce this automatically."><input className="input" type="number" min={500000} max={10000000} step={100000} value={draft.point_budget} onChange={(event) => setDraft((current) => ({ ...current, point_budget: Number(event.target.value) }))} /></Field>
                <Field label="Decoded tile cache (MiB)" error={validation.decoded_cache_mib ?? undefined}><input className="input" type="number" min={128} max={4096} step={64} value={draft.decoded_cache_mib} onChange={(event) => setDraft((current) => ({ ...current, decoded_cache_mib: Number(event.target.value) }))} /></Field>
                <Toggle label="Continue live processing in background tabs" checked={draft.continue_live_in_background} onChange={(event) => setDraft((current) => ({ ...current, continue_live_in_background: event.target.checked }))} />
                <Field label="Log level"><select className="select" value={draft.log_level ?? "INFO"} onChange={(event) => setDraft((current) => ({ ...current, log_level: event.target.value }))}><option>INFO</option><option>DEBUG</option><option>WARNING</option></select></Field>
                <div className="button-row"><Button variant="primary" busy={saving} disabled={!dirty || !valid} onClick={() => void save()}>Save settings</Button><Button disabled={!dirty} onClick={() => setDraft(settings ?? defaultSettings)}>Discard</Button><Button onClick={() => setDraft(defaultSettings)}>Reset preferences</Button></div>
              </Card>
              <Card title="Storage & support" eyebrow="LOCAL DATA">
                <div className="metric-grid"><Metric label="Disk free" value={formatBytes(resources?.disk_free_bytes)} tone={(resources?.disk_free_bytes ?? Infinity) < 20 * 1024 ** 3 ? "warning" : undefined} /><Metric label="Project data" value={formatBytes(resources?.project_size_bytes)} /></div>
                <div className="button-stack"><Button onClick={() => setMigrateOpen(true)}>Migrate data root…</Button><Button busy={running} onClick={() => void supportBundle()}>Create redacted support bundle</Button><Button onClick={() => void diagnosticWorkflowApi.revealLogs().catch((value) => setError(errorMessage(value)))}>Reveal log directory</Button></div>
                <p className="muted">Migration is a checksummed background job. Install/temp overlaps and unsafe paths are rejected.</p>
              </Card>
            </aside>
          </div>
          <Card title="Recent local logs" eyebrow="COLLAPSED BY DEFAULT"><details className="log-panel"><summary>Show last {logs.length} redacted events</summary>{logs.length ? <div className="log-table">{logs.map((line, index) => <div key={`${line.timestamp}-${index}`}><time>{formatDate(line.timestamp)}</time><StatusBadge state={line.level.toLowerCase()} label={line.level} /><code>{line.component}.{line.event}</code><span>{line.message}</span>{line.trace_id ? <small>trace {line.trace_id}</small> : null}</div>)}</div> : <p className="muted">No log events available.</p>}</details></Card>
        </>
      )}
      <Modal open={migrateOpen} title="Migrate local data root" description="This creates a durable background job and never deletes the source until every checksum and database integrity check passes." onRequestClose={() => setMigrateOpen(false)} size="sm" footer={<><Button onClick={() => setMigrateOpen(false)}>Cancel</Button><Button variant="primary" busy={running} disabled={!dataRoot.trim()} onClick={() => void migrate()}>Validate & queue migration</Button></>}>
        <InlineAlert tone="warning" title="Application restart required">Live sessions and heavy jobs must be stopped. Network and removable destinations are disabled by default.</InlineAlert><Field label="Destination directory" hint="Use a writable local absolute Windows path."><TextInput value={dataRoot} onChange={(event) => setDataRoot(event.target.value)} placeholder="D:\SpatialProbeAtlasData" /></Field>
      </Modal>
    </main>
  );
}

function DiagnosticRow({ check }: { check: DiagnosticCheck }) {
  return <details className={`diagnostic-row diagnostic-row--${check.state}`}><summary><StatusBadge state={check.state} /><span><strong>{check.name}</strong><small>{check.detail}</small></span><time>{formatDate(check.checked_at)}</time></summary>{check.impact || check.fix ? <div><p>{check.impact}</p>{check.fix ? <InlineAlert tone={check.state === "fail" ? "danger" : "info"} title="Recommended action">{check.fix}</InlineAlert> : null}</div> : null}</details>;
}
