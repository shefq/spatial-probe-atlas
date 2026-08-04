import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import { ReconnectingSocket, type BinaryStreamMessage } from "../api/streams";
import type { PaintedPoint, PaintedRecord, PreflightCheck, SessionSnapshot, SessionState, TrackingViewFrame, WsEnvelope } from "../api/types";
import { SpatialViewer, type SpatialViewerHandle } from "../viewer/react/SpatialViewer";
import { Button, Card, EmptyState, Field, InlineAlert, Metric, Modal, Segmented, Skeleton, StatusBadge, TextInput, Toggle } from "../components/ui";
import { useCameraStore, useLiveSessionStore, useProjectStore, useUiStore } from "../stores";
import { formatBytes, formatCoordinate, formatCount, formatDate, formatDuration } from "../utils/format";

export function LivePaintingPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const project = useProjectStore((state) => state.activeProject);
  const activeMap = useProjectStore((state) => state.activeMap);
  const cameraReady = useCameraStore((state) => state.status.state === "ready");
  const session = useLiveSessionStore((state) => state.session);
  const setSession = useLiveSessionStore((state) => state.setSession);
  const tracking = useLiveSessionStore((state) => state.trackingSummary);
  const setTracking = useLiveSessionStore((state) => state.setTrackingSummary);
  const reconnectState = useLiveSessionStore((state) => state.reconnectState);
  const setReconnectState = useLiveSessionStore((state) => state.setReconnectState);
  const paintingMode = useLiveSessionStore((state) => state.paintingMode);
  const setPaintingMode = useLiveSessionStore((state) => state.setPaintingMode);
  const samplingMode = useLiveSessionStore((state) => state.samplingMode);
  const setSamplingMode = useLiveSessionStore((state) => state.setSamplingMode);
  const sampleIntervalMs = useLiveSessionStore((state) => state.sampleIntervalMs);
  const sampleDistanceMm = useLiveSessionStore((state) => state.sampleDistanceMm);
  const setSampling = useLiveSessionStore((state) => state.setSampling);
  const setCounts = useLiveSessionStore((state) => state.setCounts);
  const pushToast = useUiStore((state) => state.pushToast);
  const units = useUiStore((state) => state.displayUnits);
  const viewerRef = useRef<SpatialViewerHandle>(null);
  const streamRef = useRef<ReconnectingSocket | null>(null);
  const lastSummaryAt = useRef(0);
  const [sessions, setSessions] = useState<SessionSnapshot[]>([]);
  const [recent, setRecent] = useState<PaintedRecord[]>([]);
  const [pathRecording, setPathRecording] = useState(false);
  const [sessionName, setSessionName] = useState(`Acquisition ${new Date().toLocaleDateString()}`);
  const [note, setNote] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lowQualityOpen, setLowQualityOpen] = useState(false);
  const [overrideReason, setOverrideReason] = useState("");
  const [backgroundContinue, setBackgroundContinue] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    api.sessions.list(projectId, controller.signal).then(async (values) => {
      const snapshots = await Promise.all(values.slice(0, 20).map((value) => api.sessions.get(projectId, value.id, controller.signal).catch(() => value as SessionSnapshot)));
      setSessions(snapshots);
      const resumable = snapshots.find((value) => ["running", "paused", "degraded", "recoverable", "draft", "preflight", "stopped"].includes(value.state));
      if (resumable) { setSession(resumable); setRecent(resumable.recent_records ?? []); setCounts(resumable.point_count ?? 0, resumable.path_count ?? 0); }
    }).catch((value) => { if (!controller.signal.aborted) setError(errorMessage(value)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [projectId, setCounts, setSession]);

  useEffect(() => {
    if (!session?.id || !["running", "paused", "degraded"].includes(session.state)) return;
    const stream = new ReconnectingSocket(`/ws/v1/projects/${projectId}/sessions/${session.id}/tracking`, {
      onState: setReconnectState,
      onOpen: (reconnected) => {
        if (!reconnected) return;
        setPathRecording(false);
        void api.sessions.trackingSnapshot(projectId, session.id).then((snapshot) => {
          setSession(snapshot); setRecent(snapshot.recent_records ?? []); setCounts(snapshot.point_count ?? 0, snapshot.path_count ?? 0);
          if (snapshot.tracking) applyTracking(snapshot.tracking);
        }).catch((value) => setError(errorMessage(value)));
      },
      onSequenceGap: () => void api.sessions.trackingSnapshot(projectId, session.id).then((snapshot) => { setSession(snapshot); if (snapshot.tracking) applyTracking(snapshot.tracking); }),
      onEnvelope: handleStreamEnvelope,
      onError: (message) => setError(message),
    });
    streamRef.current = stream; stream.connect(); stream.send("subscribe", { session_id: session.id });
    return () => { stream.close(); streamRef.current = null; setReconnectState("closed"); };
  }, [projectId, session?.id, session?.state]);
  useEffect(() => {
    if (!session?.started_at || !["running", "paused", "degraded"].includes(session.state)) return;
    const update = () => setElapsed(Math.max(0, (Date.now() - new Date(session.started_at!).valueOf()) / 1000));
    update(); const timer = window.setInterval(update, 1000); return () => window.clearInterval(timer);
  }, [session?.started_at, session?.state]);
  useEffect(() => {
    const onVisibility = () => {
      if (!document.hidden || backgroundContinue || session?.state !== "running") return;
      if (pathRecording) stopPath("background_pause");
      void changeLifecycle("pause");
    };
    document.addEventListener("visibilitychange", onVisibility); return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [backgroundContinue, pathRecording, session?.state]);

  function applyTracking(frame: TrackingViewFrame) {
    viewerRef.current?.applyTrackingFrame(frame);
    const now = performance.now();
    if (now - lastSummaryAt.current >= 100) { setTracking(frame); lastSummaryAt.current = now; }
    if ((frame.camera_state === "lost" || frame.probe_state === "lost") && pathRecording) {
      stopPath("tracking_lost");
      pushToast({ kind: "warning", title: "Painting auto-paused", message: "Tracking was lost; committed samples were preserved." });
    }
  }
  function handleStreamEnvelope(envelope: WsEnvelope) {
    if (envelope.type === "tracking.frame") applyTracking(envelope.data as TrackingViewFrame);
    else if (envelope.type === "tracking.lost") {
      setTracking((envelope.data as TrackingViewFrame) ?? null); setPathRecording(false);
    } else if (envelope.type.startsWith("paint.point_") || envelope.type.startsWith("paint.path_") || envelope.type === "paint.undo_committed") {
      const data = envelope.data as { record?: PaintedRecord; command_id?: string; reason?: string; point_count?: number; path_count?: number };
      if (data.command_id) viewerRef.current?.setPaintData({ removeIds: [data.command_id] });
      if (data.record) {
        setRecent((current) => [data.record!, ...current.filter((item) => item.id !== data.record!.id)].slice(0, 30));
        viewerRef.current?.setPaintData({ upsert: [data.record] });
      }
      if (data.point_count !== undefined || data.path_count !== undefined) setCounts(data.point_count ?? session?.point_count ?? 0, data.path_count ?? session?.path_count ?? 0);
      if (envelope.type.endsWith("rejected")) pushToast({ kind: "warning", title: "Paint sample rejected", message: data.reason });
    } else if (envelope.type === "session.status") setSession({ ...session!, ...(envelope.data as Partial<SessionSnapshot>) });
  }

  const fallbackPreflight: PreflightCheck[] = useMemo(() => [
    { key: "camera", label: "Record3D or replay ready", passed: cameraReady, required_route: `/projects/${projectId}/camera` },
    { key: "map", label: "Active point-cloud map", passed: Boolean(project?.active_map_id), required_route: `/projects/${projectId}/mapping` },
    { key: "calibration", label: "Active probe calibration", passed: Boolean(project?.active_probe_calibration_id), required_route: `/projects/${projectId}/registration` },
    { key: "registration", label: "Validated metric registration", passed: Boolean(project?.active_registration_id), required_route: `/projects/${projectId}/registration` },
    { key: "storage", label: "Storage admission check", passed: project?.readiness?.storage_ready !== false, required_route: "/settings" },
  ], [cameraReady, project, projectId]);
  const preflight = session?.preflight ?? fallbackPreflight;
  const preflightPassed = preflight.every((check) => check.passed);

  const createSession = async () => {
    setBusy(true); setError(null);
    try { const created = await api.sessions.create(projectId, sessionName.trim() || "Live acquisition"); setSession(created); setSessions((items) => [created, ...items]); setRecent([]); setCounts(0, 0); }
    catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const changeLifecycle = async (action: "start" | "pause" | "resume" | "stop" | "finalize") => {
    if (!session) return;
    setBusy(true); setError(null);
    try {
      const updated = await api.sessions.lifecycle(projectId, session.id, action); setSession(updated);
      if (action === "stop") setPathRecording(false);
      if (action === "finalize") navigate(`/projects/${projectId}/sessions/${session.id}/review`);
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const sendPoint = (reason?: string) => {
    if (!tracking?.tip_w_m || !streamRef.current) return;
    const commandId = streamRef.current.send("paint.point", { reason, allow_low_quality: Boolean(reason), frame_id: tracking.frame_id });
    viewerRef.current?.setPaintData({ provisional: [{ id: commandId, position: tracking.tip_w_m, quality: tracking.quality }] });
  };
  const savePoint = () => {
    if (!tracking || tracking.camera_state !== "tracked" || tracking.probe_state !== "tracked") return;
    if (["low", "warning"].includes(tracking.quality)) { setLowQualityOpen(true); return; }
    sendPoint();
  };
  const startPath = () => {
    if (!streamRef.current || !tracking || tracking.quality === "lost") return;
    streamRef.current.send("paint.path.start", { sampling: samplingMode === "time" ? { mode: "time", interval_ms: sampleIntervalMs } : { mode: "distance", distance_m: sampleDistanceMm / 1000 } });
    setPathRecording(true);
  };
  const stopPath = (reason = "user") => { streamRef.current?.send("paint.path.stop", { reason }); setPathRecording(false); };
  const undo = () => streamRef.current?.send("paint.undo", {});
  const saveNote = () => { if (!note.trim()) return; streamRef.current?.send("paint.note", { text: note.trim() }); setNote(""); };
  const state = session?.state;
  const canPaint = state === "running" && tracking?.camera_state === "tracked" && tracking.probe_state === "tracked" && reconnectState === "open";

  if (loading) return <div className="page"><Skeleton lines={9} /></div>;
  return (
    <div className="page page--workflow page--live">
      <header className="page-heading"><div><div className="eyebrow">STEP 4 · ACQUISITION</div><h1>Live Tissue Painting</h1><p>Monitor localization and probe quality while committing map-frame points and sampled paths.</p></div><div className="session-clock"><StatusBadge state={state ?? "not_started"} /><strong>{formatDuration(elapsed)}</strong></div></header>
      {error ? <InlineAlert tone="danger" title="Live session needs attention" action={<Button size="sm" onClick={() => setError(null)}>Dismiss</Button>}>{error} Committed records remain preserved.</InlineAlert> : null}
      {!session ? (
        <div className="preflight-layout">
          <Card title="New live session" eyebrow="IMMUTABLE INPUT REVISIONS"><Field label="Session name"><TextInput value={sessionName} maxLength={120} onChange={(event) => setSessionName(event.target.value)} /></Field><Button variant="primary" busy={busy} disabled={!preflightPassed} onClick={() => void createSession()}>Create session & run preflight</Button></Card>
          <Preflight checks={fallbackPreflight} />
          {sessions.filter((value) => value.state === "finalized").length ? <Card title="Recent completed sessions">{sessions.filter((value) => value.state === "finalized").slice(0, 5).map((value) => <Link className="session-link" key={value.id} to={`/projects/${projectId}/sessions/${value.id}/review`}><span><strong>{value.name}</strong><small>{formatDate(value.created_at)}</small></span><span>{formatCount(value.point_count)} points →</span></Link>)}</Card> : null}
        </div>
      ) : (
        <>
          {["draft", "preflight", "recoverable"].includes(state ?? "") ? <PreflightBanner session={session} checks={preflight} busy={busy} onStart={() => void changeLifecycle(state === "recoverable" ? "resume" : "start")} /> : null}
          <div className="live-layout">
            <div className="live-viewer-wrap">
              {(session.map_id ?? activeMap?.id) ? <SpatialViewer ref={viewerRef} mode="live" projectId={projectId} mapId={(session.map_id ?? activeMap!.id)!} sessionId={session.id} /> : <EmptyState title="Session map unavailable">Return to mapping without changing this recoverable session.</EmptyState>}
              <LiveImageOverlay active={Boolean(["running", "paused", "degraded"].includes(state ?? ""))} tracking={tracking} />
              <div className="live-quality-ribbon"><StatusBadge state={tracking?.camera_state ?? "lost"} label={`Camera ${tracking?.camera_state ?? "waiting"}`} /><StatusBadge state={tracking?.probe_state ?? "lost"} label={`Probe ${tracking?.probe_state ?? "waiting"}`} /><StatusBadge state={tracking?.quality ?? "inactive"} label={`Quality ${tracking?.quality ?? "—"}`} /><StatusBadge state={reconnectState} label={`Stream ${reconnectState}`} /></div>
            </div>
            <aside className="live-controls">
              <Card title="Session controls" eyebrow={session.name}>
                <div className="button-row live-lifecycle">{state === "running" ? <Button onClick={() => void changeLifecycle("pause")} busy={busy}>Ⅱ Pause</Button> : state === "paused" || state === "degraded" || state === "recoverable" ? <Button variant="primary" onClick={() => void changeLifecycle("resume")} busy={busy}>▶ Resume</Button> : null}{["running", "paused", "degraded"].includes(state ?? "") ? <Button variant="danger" onClick={() => void changeLifecycle("stop")} busy={busy}>■ Stop</Button> : null}{state === "stopped" ? <Button variant="primary" onClick={() => void changeLifecycle("finalize")} busy={busy}>Finalize & review</Button> : null}</div>
                <Toggle label="Continue live processing in background" checked={backgroundContinue} onChange={(event) => setBackgroundContinue(event.target.checked)} />
              </Card>
              <Card title="Tip position" eyebrow="WORLD FRAME W · METRES STORED"><div className="coordinate-grid"><span><small>X</small>{formatCoordinate(tracking?.tip_w_m?.[0], units)}</span><span><small>Y</small>{formatCoordinate(tracking?.tip_w_m?.[1], units)}</span><span><small>Z</small>{formatCoordinate(tracking?.tip_w_m?.[2], units)}</span></div><div className="metric-grid"><Metric label="Camera inliers" value={tracking?.camera_inliers ?? "—"} /><Metric label="Probe inliers" value={tracking?.probe_inliers == null ? "—" : `${tracking.probe_inliers}/5`} /><Metric label="FPS" value={tracking?.fps?.toFixed(1) ?? "—"} /><Metric label="Latency" value={tracking?.latency_ms == null ? "—" : `${tracking.latency_ms.toFixed(0)} ms`} tone={(tracking?.latency_ms ?? 0) > 100 ? "warning" : undefined} /></div></Card>
              <Card title="Painting" eyebrow={pathRecording ? "PATH RECORDING" : "READY"} actions={<StatusBadge state={canPaint ? "ready" : "inactive"} label={canPaint ? "Quality gate passed" : "Waiting"} />}>
                <Segmented value={paintingMode} label="Painting mode" options={[{ value: "point", label: "Point" }, { value: "path", label: "Path" }]} onChange={setPaintingMode} />
                {paintingMode === "path" ? <><Segmented value={samplingMode} label="Path sampling mode" options={[{ value: "distance", label: "Distance" }, { value: "time", label: "Time" }]} onChange={setSamplingMode} /><Field label={samplingMode === "distance" ? "Sample spacing (mm)" : "Sample interval (ms)"}><input className="input" type="number" min={samplingMode === "distance" ? 0.1 : 20} max={samplingMode === "distance" ? 100 : 5000} value={samplingMode === "distance" ? sampleDistanceMm : sampleIntervalMs} onChange={(event) => setSampling(Number(event.target.value))} /></Field></> : null}
                <div className="paint-primary">{paintingMode === "point" ? <Button variant="primary" disabled={!canPaint} onClick={savePoint}>＋ Save point</Button> : pathRecording ? <Button variant="danger" onClick={() => stopPath()}>■ Stop path</Button> : <Button variant="primary" disabled={!canPaint} onClick={startPath}>● Start path</Button>}<Button disabled={!recent.length} onClick={undo}>↶ Undo last</Button><Button onClick={() => viewerRef.current?.resetView()}>Focus probe</Button></div>
                <div className="paint-counts"><Metric label="Points" value={formatCount(session.point_count)} /><Metric label="Paths" value={formatCount(session.path_count)} /><Metric label="Session size" value={formatBytes(session.size_bytes)} /></div>
              </Card>
              <Card title="Session note" eyebrow="TIMESTAMPED"><div className="inline-form"><TextInput value={note} maxLength={1000} placeholder="Add an observation…" onChange={(event) => setNote(event.target.value)} /><Button disabled={!note.trim()} onClick={saveNote}>Add</Button></div></Card>
            </aside>
          </div>
          <Card title="Recent committed records" eyebrow="SERVER AUTHORITATIVE"><RecentRecords records={recent} units={units} /></Card>
        </>
      )}
      <Modal open={lowQualityOpen} title="Save a low-quality point?" description="An explicit reason is required and will be exported with the flagged record." onRequestClose={() => setLowQualityOpen(false)} size="sm" footer={<><Button onClick={() => setLowQualityOpen(false)}>Cancel</Button><Button variant="danger" disabled={overrideReason.trim().length < 3} onClick={() => { sendPoint(overrideReason.trim()); setOverrideReason(""); setLowQualityOpen(false); }}>Save flagged point</Button></>}><Field label="Reason" hint="At least 3 characters; do not include patient-identifying information."><TextInput value={overrideReason} maxLength={240} onChange={(event) => setOverrideReason(event.target.value)} placeholder="e.g. Intentional edge sample" /></Field></Modal>
    </div>
  );
}

function Preflight({ checks }: { checks: PreflightCheck[] }) {
  return <Card title="Preflight" eyebrow="ALL REQUIRED"><div className="preflight-checks">{checks.map((check) => <div key={check.key} className={check.passed ? "is-passed" : "is-failed"}><span>{check.passed ? "✓" : "×"}</span><div><strong>{check.label}</strong>{check.detail ? <small>{check.detail}</small> : null}</div>{!check.passed && check.required_route ? <Link to={check.required_route}>Resolve →</Link> : null}</div>)}</div></Card>;
}

function PreflightBanner({ session, checks, busy, onStart }: { session: SessionSnapshot; checks: PreflightCheck[]; busy: boolean; onStart: () => void }) {
  const passed = checks.every((check) => check.passed);
  return <InlineAlert tone={passed ? "success" : "warning"} title={session.state === "recoverable" ? "Recoverable session found" : passed ? "Preflight passed" : "Preflight blocked"} action={<Button variant="primary" busy={busy} disabled={!passed && session.state !== "recoverable"} onClick={onStart}>{session.state === "recoverable" ? "Resume session" : "Start session"}</Button>}>{passed ? "The exact map, probe and registration revisions are locked for this session." : checks.filter((check) => !check.passed).map((check) => check.label).join(" · ")}</InlineAlert>;
}

function RecentRecords({ records, units }: { records: PaintedRecord[]; units: "mm" | "m" }) {
  if (!records.length) return <p className="muted">No committed paint records yet.</p>;
  return <div className="data-table"><div className="data-table__head"><span>Time</span><span>Type</span><span>Position / samples</span><span>Quality</span><span>Note</span></div>{records.slice(0, 12).map((record) => <div className="data-table__row" key={record.id}><span>{formatDate(record.type === "point" ? record.timestamp : record.started_at)}</span><span>{record.type}</span><span>{record.type === "point" ? record.position_w_m.map((value) => formatCoordinate(value, units)).join(" · ") : `${record.sample_count} samples`}</span><span><StatusBadge state={record.quality} /></span><span>{record.note ?? "—"}</span></div>)}</div>;
}

function LiveImageOverlay({ active, tracking }: { active: boolean; tracking: TrackingViewFrame | null }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!active) return;
    let current: string | null = null;
    const stream = new ReconnectingSocket("/ws/v1/camera/preview", {
      onBinary: (message: BinaryStreamMessage) => {
        const encoding = String(message.header.encoding ?? "png").toLowerCase();
        const next = URL.createObjectURL(new Blob([message.payload], { type: encoding.includes("png") ? "image/png" : "image/jpeg" }));
        if (current) URL.revokeObjectURL(current); current = next; setUrl(next);
      },
    });
    stream.connect(); stream.send("subscribe", { channels: ["rgb"], quality: "low", overlay: true });
    return () => { stream.close(); if (current) URL.revokeObjectURL(current); setUrl(null); };
  }, [active]);
  return <div className="live-image-overlay">{url ? <img src={url} alt="Live camera tracking overlay" /> : <span>Camera overlay</span>}<div><StatusBadge state={tracking?.probe_state ?? "lost"} label={tracking?.probe_state === "tracked" ? "Probe visible" : "Probe lost"} /></div></div>;
}
