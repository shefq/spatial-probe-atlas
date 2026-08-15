import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import { ReconnectingSocket, type BinaryStreamMessage } from "../api/streams";
import type { PaintedPoint, PaintedRecord, PreflightCheck, SessionSnapshot, SessionState, TrackingViewFrame, WsEnvelope } from "../api/types";
import { SpatialViewer, type SpatialViewerHandle } from "../viewer/react/SpatialViewer";
import { ManualAnnotationModal } from "../components/ManualAnnotationModal";
import { Button, Card, EmptyState, Field, InlineAlert, Metric, Modal, Segmented, Skeleton, StatusBadge, TextInput, Toggle } from "../components/ui";
import { useCameraStore, useLiveSessionStore, useProjectStore, useUiStore } from "../stores";
import { formatBytes, formatCoordinate, formatCount, formatDate, formatDuration } from "../utils/format";

export function LivePaintingPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const project = useProjectStore((state) => state.activeProject);
  const activeMap = useProjectStore((state) => state.activeMap);
  const cameraReady = useCameraStore((state) => state.status.state === "ready");
  const cameraStatus = useCameraStore((state) => state.status);
  const cameraIntrinsics = useMemo(() => {
    if (cameraStatus.intrinsic_matrix && cameraStatus.rgb_width && cameraStatus.rgb_height) {
      return { matrix: cameraStatus.intrinsic_matrix, width: cameraStatus.rgb_width, height: cameraStatus.rgb_height };
    }
    return undefined;
  }, [cameraStatus]);
  const session = useLiveSessionStore((state) => state.session);
  const setSession = useLiveSessionStore((state) => state.setSession);
  const tracking = useLiveSessionStore((state) => state.trackingSummary);
  const setTracking = useLiveSessionStore((state) => state.setTrackingSummary);
  const reconnectState = useLiveSessionStore((state) => state.reconnectState);
  const setReconnectState = useLiveSessionStore((state) => state.setReconnectState);
  const setCounts = useLiveSessionStore((state) => state.setCounts);
  const pushToast = useUiStore((state) => state.pushToast);
  const units = useUiStore((state) => state.displayUnits);
  const viewerRef = useRef<SpatialViewerHandle>(null);
  const streamRef = useRef<ReconnectingSocket | null>(null);
  const lastSummaryAt = useRef(0);
  const [sessions, setSessions] = useState<SessionSnapshot[]>([]);
  const [recent, setRecent] = useState<PaintedRecord[]>([]);

  const [sessionName, setSessionName] = useState(`Acquisition ${new Date().toLocaleDateString()}`);
  const [note, setNote] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lowQualityOpen, setLowQualityOpen] = useState(false);
  const [overrideReason, setOverrideReason] = useState("");
  const [backgroundContinue, setBackgroundContinue] = useState(false);
  const [probeGeometry, setProbeGeometry] = useState<number[][] | undefined>();
  const [boardDefinition, setBoardDefinition] = useState<any>();
  const [isArucoMode, setIsArucoMode] = useState<boolean>(false);
  const [annotateRecord, setAnnotateRecord] = useState<PaintedRecord | null>(null);
  const [windowSec, setWindowSec] = useState(0.5);
  const [useWindowAvg, setUseWindowAvg] = useState(false);
  const viewMode = useUiStore((state) => state.viewMode);
  const setViewMode = useUiStore((state) => state.setViewMode);

  useEffect(() => {
    if (!project?.active_probe_calibration_id) return;
    const controller = new AbortController();
    api.probe.get(projectId, project.active_probe_calibration_id, controller.signal).then(cal => {
      setProbeGeometry(cal.probe?.marker_points_m);
    }).catch(() => {});
    return () => controller.abort();
  }, [projectId, project?.active_probe_calibration_id]);

  useEffect(() => {
    if (!project?.active_registration_id) return;
    const controller = new AbortController();
    api.registration.get(projectId, project.active_registration_id, controller.signal).then(reg => {
      setBoardDefinition((reg as any).board_definition);
      setIsArucoMode((reg as any).is_aruco_mode);
    }).catch(() => {});
    return () => controller.abort();
  }, [projectId, project?.active_registration_id]);

  const registrationView = useMemo(() => ({
    board_definition: boardDefinition,
    is_aruco_mode: isArucoMode
  }), [boardDefinition, isArucoMode]);

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
      void changeLifecycle("pause");
    };
    document.addEventListener("visibilitychange", onVisibility); return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [backgroundContinue, session?.state]);

  function applyTracking(frame: TrackingViewFrame) {
    viewerRef.current?.applyTrackingFrame(frame);
    const now = performance.now();
    if (now - lastSummaryAt.current >= 100) { setTracking(frame); lastSummaryAt.current = now; }
  }
  function handleStreamEnvelope(envelope: WsEnvelope) {
    if (envelope.type === "tracking.frame") applyTracking(envelope.data as TrackingViewFrame);
    else if (envelope.type === "tracking.lost") {
      setTracking((envelope.data as TrackingViewFrame) ?? null);
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

  const fallbackPreflight: PreflightCheck[] = useMemo(() => {
    const isAruco = (localStorage.getItem("spa_workflow_mode") as "standard" | "aruco_joint") === "aruco_joint";
    const hasMap = isAruco || Boolean(project?.active_map_id);
    const hasProbe = Boolean(project?.active_probe_calibration_id);
    const hasReg = Boolean(project?.active_registration_id);
    return [
      { key: "camera", label: "Record3D or replay ready", passed: cameraReady, required_route: `/projects/${projectId}/camera` },
      { key: "map", label: isAruco ? "ArUco Board Registration" : "Active point-cloud map", passed: hasMap, required_route: `/projects/${projectId}/mapping` },
      { key: "calibration", label: "Active probe calibration", passed: hasProbe, required_route: `/projects/${projectId}/registration` },
      { key: "registration", label: "Validated metric registration", passed: hasReg, required_route: `/projects/${projectId}/registration` },
      { key: "dependency_binding", label: "Registration bound to active map & probe", passed: hasMap && hasProbe && hasReg, required_route: `/projects/${projectId}/registration` },
      { key: "storage", label: "Storage admission check", passed: project?.readiness?.storage_ready !== false, required_route: "/settings" },
    ];
  }, [cameraReady, project, projectId]);
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
      if (action === "finalize") navigate(`/projects/${projectId}/sessions/${session.id}/review`);
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const sendPoint = (reason?: string) => {
    if (!streamRef.current) return;
    const commandId = streamRef.current.send("paint.point", {
      reason,
      allow_low_quality: true,
      frame_id: tracking?.frame_id,
      save_image: true,
      window_s: windowSec,
      use_window_average: useWindowAvg,
    });
    if (tracking?.tip_w_m && tracking.probe_state === "tracked" && tracking.camera_state === "tracked") {
      viewerRef.current?.setPaintData({ provisional: [{ id: commandId, position: tracking.tip_w_m, quality: tracking.quality }] });
    }
  };
  const savePoint = () => {
    sendPoint();
  };
  const undo = () => streamRef.current?.send("paint.undo", {});
  const saveNote = () => { if (!note.trim()) return; streamRef.current?.send("paint.note", { text: note.trim() }); setNote(""); };
  const state = session?.state;
  const cameraTracked = tracking?.camera_state === "tracked";
  const probeTracked = tracking?.probe_state === "tracked";
  // With a time window > 0, we allow saving even if the probe is lost right now
  // (the backend will search the buffer). Camera must always be tracked.
  const canPaint = state === "running" && reconnectState === "open" && cameraTracked && (probeTracked || windowSec > 0);

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
              {((session.map_id ?? activeMap?.id) || (localStorage.getItem("spa_workflow_mode") === "aruco_joint")) ? <SpatialViewer ref={viewerRef} mode="live" projectId={projectId} mapId={(session.map_id ?? activeMap?.id) || ""} sessionId={session.id} probeGeometry={probeGeometry} registration={registrationView} cameraIntrinsics={cameraIntrinsics} /> : <EmptyState title="Session map unavailable">Return to mapping without changing this recoverable session.</EmptyState>}
              <LiveImageOverlay active={Boolean(["running", "paused", "degraded"].includes(state ?? ""))} tracking={tracking} />
              <div className="live-quality-ribbon"><StatusBadge state={tracking?.camera_state ?? "lost"} label={`Camera ${tracking?.camera_state ?? "waiting"}`} /><StatusBadge state={tracking?.probe_state ?? "lost"} label={`Probe ${tracking?.probe_state ?? "waiting"}`} /><StatusBadge state={tracking?.quality ?? "inactive"} label={`Quality ${tracking?.quality ?? "—"}`} /><StatusBadge state={reconnectState} label={`Stream ${reconnectState}`} /></div>
            </div>
            <aside className="live-controls">
              <Card title="Session controls" eyebrow={session.name} actions={<Segmented label="View mode" value={viewMode} options={[{ value: "points", label: "Points" }, { value: "mesh", label: "Mesh" }]} onChange={(v) => setViewMode(v as "points" | "mesh")} />}>
                <div className="button-row live-lifecycle">{state === "running" ? <Button onClick={() => void changeLifecycle("pause")} busy={busy}>Ⅱ Pause</Button> : state === "paused" || state === "degraded" || state === "recoverable" ? <Button variant="primary" onClick={() => void changeLifecycle("resume")} busy={busy}>▶ Resume</Button> : null}{["running", "paused", "degraded"].includes(state ?? "") ? <Button variant="danger" onClick={() => void changeLifecycle("stop")} busy={busy}>■ Stop</Button> : null}{state === "stopped" ? <Button variant="primary" onClick={() => void changeLifecycle("finalize")} busy={busy}>Finalize & review</Button> : null}</div>
                <Toggle label="Continue live processing in background" checked={backgroundContinue} onChange={(event) => setBackgroundContinue(event.target.checked)} />
              </Card>
              <Card title="Tip position" eyebrow="WORLD FRAME W · METRES STORED"><div className="coordinate-grid"><span><small>X</small>{formatCoordinate(tracking?.tip_w_m?.[0], units)}</span><span><small>Y</small>{formatCoordinate(tracking?.tip_w_m?.[1], units)}</span><span><small>Z</small>{formatCoordinate(tracking?.tip_w_m?.[2], units)}</span></div><div className="metric-grid"><Metric label="Camera inliers" value={tracking?.camera_inliers ?? "—"} /><Metric label="Probe inliers" value={tracking?.probe_inliers == null ? "—" : `${tracking.probe_inliers}/5`} /><Metric label="FPS" value={tracking?.fps?.toFixed(1) ?? "—"} /><Metric label="Latency" value={tracking?.latency_ms == null ? "—" : `${tracking.latency_ms.toFixed(0)} ms`} tone={(tracking?.latency_ms ?? 0) > 100 ? "warning" : undefined} /></div></Card>
              <Card title="Painting" eyebrow="READY" actions={<StatusBadge state={canPaint ? "ready" : "inactive"} label={canPaint ? "Ready" : "Waiting"} />}>
                <div className="paint-primary"><div style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}><Button variant="primary" disabled={!canPaint} onClick={savePoint}>＋ Save point</Button>{state === "running" && !cameraTracked ? <small className="color-warning" style={{ color: "#f2bd55", fontWeight: 500, fontSize: "11px" }}>Camera tracking lost</small> : state === "running" && cameraTracked && !probeTracked && windowSec > 0 ? <small style={{ color: "#7dd3fc", fontWeight: 500, fontSize: "11px" }}>Window capture (±{windowSec.toFixed(1)} s)</small> : null}</div><Button disabled={!recent.length} onClick={undo}>↶ Undo last</Button><Button onClick={() => viewerRef.current?.resetView()}>Focus probe</Button></div>
                <div className="paint-counts"><Metric label="Points" value={formatCount(session.point_count)} /><Metric label="Session size" value={formatBytes(session.size_bytes)} /></div>
                <div style={{ marginTop: "12px", borderTop: "1px solid #2a2a2a", paddingTop: "12px", display: "flex", flexDirection: "column", gap: "10px" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                    <label style={{ fontSize: "12px", color: "#888", whiteSpace: "nowrap" }}>Search window (s)</label>
                    <input
                      type="number" min="0" max="5" step="0.1"
                      value={windowSec}
                      onChange={(e) => setWindowSec(Math.max(0, Math.min(5, parseFloat(e.target.value) || 0)))}
                      style={{ width: "64px", background: "#1a1a1a", border: "1px solid #333", borderRadius: "6px", color: "#e5e5e5", padding: "4px 8px", fontSize: "13px" }}
                    />
                    <small style={{ color: "#555", fontSize: "11px" }}>±window around click</small>
                  </div>
                  <Toggle label="Use averaged probe position from window" checked={useWindowAvg} onChange={(e) => setUseWindowAvg(e.target.checked)} />
                </div>
              </Card>
              <Card title="Session note" eyebrow="TIMESTAMPED"><div className="inline-form"><TextInput value={note} maxLength={1000} placeholder="Add an observation…" onChange={(event) => setNote(event.target.value)} /><Button disabled={!note.trim()} onClick={saveNote}>Add</Button></div></Card>
            </aside>
          </div>
          <Card title="Recent committed records" eyebrow="SERVER AUTHORITATIVE"><RecentRecords records={recent} units={units} projectId={projectId} sessionId={session.id} onAnnotate={setAnnotateRecord} /></Card>
        </>
      )}
      <Modal open={lowQualityOpen} title="Save a low-quality point?" description="An explicit reason is required and will be exported with the flagged record." onRequestClose={() => setLowQualityOpen(false)} size="sm" footer={<><Button onClick={() => setLowQualityOpen(false)}>Cancel</Button><Button variant="danger" disabled={overrideReason.trim().length < 3} onClick={() => { sendPoint(overrideReason.trim()); setOverrideReason(""); setLowQualityOpen(false); }}>Save flagged point</Button></>}><Field label="Reason" hint="At least 3 characters; do not include patient-identifying information."><TextInput value={overrideReason} maxLength={240} onChange={(event) => setOverrideReason(event.target.value)} placeholder="e.g. Intentional edge sample" /></Field></Modal>
      <ManualAnnotationModal open={!!annotateRecord} projectId={projectId} sessionId={session?.id ?? ""} record={annotateRecord} onClose={() => setAnnotateRecord(null)} onSuccess={(updated) => { setAnnotateRecord(null); setRecent(r => [updated, ...r.filter(x => x.id !== updated.id)].slice(0, 30)); viewerRef.current?.setPaintData({ upsert: [updated] }); }} />
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

function RecentRecords({ records, units, projectId, sessionId, onAnnotate }: { records: PaintedRecord[]; units: "mm" | "m"; projectId: string; sessionId: string; onAnnotate: (record: PaintedRecord) => void }) {
  if (!records.length) return <p className="muted">No committed paint records yet.</p>;
  return <div className="data-table"><div className="data-table__head"><span>Time</span><span>Type</span><span>Position / samples</span><span>Quality</span><span>Note</span></div>{records.slice(0, 12).map((record) => <div className="data-table__row" key={record.id}><span>{formatDate(record.type === "point" ? record.timestamp : record.started_at)}</span><span>{record.type}</span><span>{record.type === "point" ? (record.position_w_m?.length === 3 ? record.position_w_m.map((value) => formatCoordinate(value, units)).join(" · ") : (record.image_uri ? <div style={{ display: "flex", alignItems: "center", gap: "12px" }}><img src={`/api/v1/projects/${projectId}/sessions/${sessionId}/painted-records/${record.id}/image`} alt="Capture" style={{ height: "40px", borderRadius: "4px", objectFit: "cover" }} /><Button size="sm" onClick={() => onAnnotate(record)}>Annotate</Button></div> : "Needs Annotation")) : `${record.sample_count} samples`}</span><span><StatusBadge state={record.quality} /></span><span>{record.note ?? "—"}</span></div>)}</div>;
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
  return (
    <div className="live-image-overlay">
      {url ? (
        <img
          src={url}
          alt="Live camera tracking overlay"
          onLoad={(e) => {
            const img = e.currentTarget;
            if (img.naturalWidth && img.naturalHeight) {
              img.parentElement?.style.setProperty("aspect-ratio", `${img.naturalWidth} / ${img.naturalHeight}`);
            }
          }}
        />
      ) : (
        <span>Camera overlay</span>
      )}
      <div><StatusBadge state={tracking?.probe_state ?? "lost"} label={tracking?.probe_state === "tracked" ? "Probe visible" : "Probe lost"} /></div>
    </div>
  );
}
