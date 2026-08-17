import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import { LatestFrameBuffer, ReconnectingSocket, type BinaryStreamMessage } from "../api/streams";
import type { CaptureFrame, CaptureSet, JobSnapshot, SceneMap } from "../api/types";
import { SpatialViewer, type SpatialViewerHandle } from "../viewer/react/SpatialViewer";
import type { ViewerMetrics } from "../viewer/engine/types";
import { Button, Card, EmptyState, Field, InlineAlert, Metric, ProgressBar, Segmented, Skeleton, StatusBadge, TextInput } from "../components/ui";
import { useCameraStore, useDiagnosticsStore, useJobStore, useProjectStore, useUiStore } from "../stores";
import { formatBytes, formatCount, formatDuration } from "../utils/format";

type CaptureMode = "manual" | "interval" | "motion";

export function MappingPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const cameraReady = useCameraStore((state) => state.status.state === "ready");
  const compute = useDiagnosticsStore((state) => state.capabilities?.effective_compute_profile ?? "cpu_sift");
  const activeProject = useProjectStore((state) => state.activeProject);
  const setActiveMap = useProjectStore((state) => state.setActiveMap);
  const jobStore = useJobStore((state) => state.jobs);
  const upsertJob = useJobStore((state) => state.upsert);
  const pushToast = useUiStore((state) => state.pushToast);
  const [captureSets, setCaptureSets] = useState<CaptureSet[]>([]);
  const [selectedSetId, setSelectedSetId] = useState("");
  const [selectedSet, setSelectedSet] = useState<(CaptureSet & { frames?: CaptureFrame[] }) | null>(null);
  const [maps, setMaps] = useState<SceneMap[]>([]);
  const [selectedMapId, setSelectedMapId] = useState(activeProject?.active_map_id ?? "");
  const [captureMode, setCaptureMode] = useState<CaptureMode>("manual");
  const [intervalSeconds, setIntervalSeconds] = useState(1.5);
  const [capturing, setCapturing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mapName, setMapName] = useState("Reference map A");
  const [computeProfile, setComputeProfile] = useState("auto");
  const [viewerMetrics, setViewerMetrics] = useState<ViewerMetrics | null>(null);
  const [showCameraPreview, setShowCameraPreview] = useState(true);
  const intervalRef = useRef<number | null>(null);
  const viewerRef = useRef<SpatialViewerHandle>(null);
  const cameraStatus = useCameraStore((state) => state.status);
  const viewMode = useUiStore((state) => state.viewMode);
  const setViewMode = useUiStore((state) => state.setViewMode);

  const refresh = async (signal?: AbortSignal) => {
    setLoading(true); setError(null);
    try {
      const [setsValue, mapsValue] = await Promise.all([api.capture.sets(projectId, signal), api.maps.list(projectId, signal)]);
      setCaptureSets(setsValue); setMaps(mapsValue);
      const setId = selectedSetId || setsValue[0]?.id || "";
      setSelectedSetId(setId);
      if (setId) setSelectedSet(await api.capture.getSet(projectId, setId));
      const mapId = selectedMapId || mapsValue.find((map) => map.active)?.id || mapsValue[0]?.id || "";
      setSelectedMapId(mapId);
      setActiveMap(mapsValue.find((map) => map.id === mapId) ?? null);
    } catch (value) { if (!signal?.aborted) setError(errorMessage(value)); }
    finally { if (!signal?.aborted) setLoading(false); }
  };
  useEffect(() => { const controller = new AbortController(); void refresh(controller.signal); return () => controller.abort(); }, [projectId]);
  useEffect(() => {
    if (!selectedSetId) { setSelectedSet(null); return; }
    void api.capture.getSet(projectId, selectedSetId).then(setSelectedSet).catch((value) => setError(errorMessage(value)));
  }, [projectId, selectedSetId]);
  useEffect(() => () => { if (intervalRef.current !== null) window.clearInterval(intervalRef.current); }, []);

  const activeJob = useMemo(() => {
    const mapJobId = maps.find((map) => map.id === selectedMapId)?.job_id;
    if (mapJobId && jobStore[mapJobId] && !["completed", "cancelled", "failed"].includes(jobStore[mapJobId].state)) return jobStore[mapJobId];
    return Object.values(jobStore).find((job) => job.owner_project_id === projectId && ["mapping", "mesh"].includes(job.type) && !["completed", "cancelled", "failed"].includes(job.state));
  }, [jobStore, maps, projectId, selectedMapId]);
  useEffect(() => {
    if (!activeJob?.id || ["completed", "cancelled", "failed"].includes(activeJob.state)) return;
    const timer = window.setInterval(() => void api.jobs.get(activeJob.id).then((job) => {
      upsertJob(job);
      if (job.state === "completed") void refresh();
    }).catch(() => undefined), 1500);
    return () => window.clearInterval(timer);
  }, [activeJob?.id, activeJob?.state]);

  const ensureCaptureSet = async (): Promise<CaptureSet> => {
    if (selectedSet) return selectedSet;
    const created = await api.capture.createSet(projectId, "Reference capture", cameraReady ? "record3d" : "import");
    setCaptureSets((current) => [created, ...current]); setSelectedSetId(created.id); setSelectedSet(created);
    return created;
  };
  const captureOne = async () => {
    const captureSet = await ensureCaptureSet();
    const frame = await api.capture.captureFrame(projectId, captureSet.id);
    setSelectedSet((current) => current ? { ...current, frame_count: current.frame_count + 1, accepted_frame_count: current.accepted_frame_count + (frame.included ? 1 : 0), frames: [...(current.frames ?? []), frame] } : current);
  };
  const toggleCapture = async () => {
    if (capturing) {
      setCapturing(false); if (intervalRef.current !== null) window.clearInterval(intervalRef.current); intervalRef.current = null; return;
    }
    setBusy(true);
    try {
      await captureOne();
      if (captureMode !== "manual") {
        setCapturing(true);
        intervalRef.current = window.setInterval(() => void captureOne().catch((value) => { setError(errorMessage(value)); setCapturing(false); }), Math.max(250, intervalSeconds * 1000));
      }
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };

  const toggleCaptureRef = useRef(toggleCapture);
  toggleCaptureRef.current = toggleCapture;

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.code === "Space" || event.key === " ") {
        const target = event.target as HTMLElement | null;
        if (target && (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable)) {
          return;
        }
        event.preventDefault();
        void toggleCaptureRef.current();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);
  const importFrames = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true);
    try {
      const captureSet = await ensureCaptureSet();
      const result = await api.capture.importFrames(projectId, captureSet.id, Array.from(files));
      pushToast({ kind: result.rejected ? "warning" : "success", title: `${result.accepted} frames imported`, message: result.rejected ? `${result.rejected} corrupt or incompatible files were rejected.` : undefined });
      setSelectedSet(await api.capture.getSet(projectId, captureSet.id));
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const toggleFrame = async (frame: CaptureFrame) => {
    if (!selectedSet) return;
    try {
      const updated = await api.capture.setFrameIncluded(projectId, selectedSet.id, frame.id, !frame.included);
      setSelectedSet((current) => current ? { ...current, frames: current.frames?.map((item) => item.id === updated.id ? updated : item), accepted_frame_count: current.accepted_frame_count + (updated.included ? 1 : -1), excluded_frame_count: current.excluded_frame_count + (updated.included ? -1 : 1) } : current);
    } catch (value) { setError(errorMessage(value)); }
  };
  const deleteFrame = async (e: React.MouseEvent, frame: CaptureFrame) => {
    e.preventDefault();
    e.stopPropagation();
    if (!selectedSet || !window.confirm(`Delete frame ${frame.sequence}?`)) return;
    try {
      await api.capture.deleteFrame(projectId, selectedSet.id, frame.id);
      setSelectedSet((current) => current ? { ...current, frames: current.frames?.filter((item) => item.id !== frame.id), frame_count: current.frame_count - 1, accepted_frame_count: current.accepted_frame_count - (frame.included ? 1 : 0), excluded_frame_count: current.excluded_frame_count - (frame.included ? 0 : 1) } : current);
    } catch (value) { setError(errorMessage(value)); }
  };
  const buildMap = async () => {
    if (!selectedSet || selectedSet.accepted_frame_count < 7) return;
    setBusy(true);
    try {
      const latestSet = await api.capture.getSet(projectId, selectedSet.id);
      const map = await api.maps.create(projectId, latestSet, computeProfile, mapName.trim() || "Reference map");
      setMaps((current) => [map, ...current]); setSelectedMapId(map.id);
      upsertJob({ id: map.job_id, type: "mapping", state: "queued", progress: 0, owner_project_id: projectId, effective_compute_profile: map.job_id ? computeProfile : compute });
      pushToast({ kind: "success", title: "Reconstruction queued", message: "Progress is durable; you can leave this page." });
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const activateMap = async (map: SceneMap) => {
    setBusy(true);
    try { await api.maps.activate(projectId, map.id); setMaps((current) => current.map((item) => ({ ...item, active: item.id === map.id }))); setActiveMap({ ...map, active: true }); pushToast({ kind: "success", title: "Reference map activated" }); }
    catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const selectedMap = maps.find((map) => map.id === selectedMapId);
  const acceptedFrames = selectedSet?.accepted_frame_count ?? 0;
  const canBuild = acceptedFrames >= 7 && !activeJob;

  return (
    <div className="page page--workflow">
      <header className="page-heading"><div><div className="eyebrow">STEP 2 · REFERENCE SPACE</div><h1>Scene Capture & Mapping</h1><p>Acquire a well-covered frame set, build a durable SfM map, then inspect and activate it.</p></div><div className="page-heading__actions">{selectedMap?.active ? <Button variant="primary" onClick={() => navigate(`/projects/${projectId}/registration`)}>Continue to registration →</Button> : null}</div></header>
      {error ? <InlineAlert tone="danger" title="Mapping action failed" action={<Button size="sm" onClick={() => setError(null)}>Dismiss</Button>}>{error}</InlineAlert> : null}
      <div className="mapping-capture-strip">
        <div className="capture-source"><StatusBadge state={cameraReady ? "ready" : "inactive"} label={cameraReady ? "Camera ready" : "Import mode"} /><select className="select" value={selectedSetId} onChange={(event) => setSelectedSetId(event.target.value)}><option value="">New capture set</option>{captureSets.map((set) => <option value={set.id} key={set.id}>{set.name} · {set.accepted_frame_count} accepted</option>)}</select></div>
        <Segmented value={captureMode} label="Capture mode" options={[{ value: "manual", label: "Manual" }, { value: "interval", label: "Interval" }, { value: "motion", label: "Motion" }]} onChange={setCaptureMode} />
        {captureMode !== "manual" ? <Field label={captureMode === "interval" ? "Interval (seconds)" : "Check interval (seconds)"}><input className="input input--small" type="number" min={0.25} max={10} step={0.25} value={intervalSeconds} onChange={(event) => setIntervalSeconds(Number(event.target.value))} /></Field> : null}
        <Button variant={capturing ? "danger" : "primary"} busy={busy} disabled={!cameraReady && !capturing} onClick={() => void toggleCapture()}>{capturing ? "■ Stop capture" : captureMode === "manual" ? "● Capture frame" : "● Start capture"}</Button>
        <label className="button button--default button--md file-button">Import frame bundle<input type="file" multiple accept=".json,application/json" onChange={(event) => void importFrames(event.target.files)} /></label>
      </div>
      <div className="workflow-grid workflow-grid--mapping">
        <div className="mapping-main">
          <Card className="viewer-card registration-workspace-card" title="Spatial map inspection" eyebrow={selectedMap ? `${selectedMap.name} · ${selectedMap.state}` : "NO MAP SELECTED"} actions={selectedMap ? <><Segmented label="View mode" value={viewMode} options={[{ value: "points", label: "Points" }, { value: "mesh", label: "Mesh" }]} onChange={(v) => setViewMode(v as "points" | "mesh")} /><select className="select select--compact" value={selectedMapId} onChange={(event) => setSelectedMapId(event.target.value)}>{maps.map((map) => <option key={map.id} value={map.id}>{map.name}{map.active ? " · active" : ""}</option>)}</select>{!selectedMap.active && selectedMap.state.startsWith("ready") ? <Button size="sm" busy={busy} onClick={() => void activateMap(selectedMap)}>Activate</Button> : null}</> : null}>
            <div className="registration-workspace-layout">
              <div className="registration-viewer-wrap">
                {selectedMap ? <SpatialViewer ref={viewerRef} mode="mapping" projectId={projectId} mapId={selectedMap.id} onMetrics={setViewerMetrics} filters={{ showMap: true, showFrames: true, showBoard: true, showMarkers: true }} /> : <div className="viewer-empty"><EmptyState icon="⌖" title="Your point cloud will appear here">Capture at least 7 accepted frames, then build the CPU or CUDA reconstruction.</EmptyState></div>}
              </div>
              <div className="registration-record3d-widget">
                <div className="record3d-widget-header">
                  <div>
                    <div className="eyebrow">LIVE RECORD3D</div>
                    <strong>Live RGB preview</strong>
                  </div>
                  <StatusBadge state={cameraStatus.state === "ready" ? "open" : cameraStatus.state} />
                </div>
                <div className="tracking-feed-preview" style={{ marginBottom: 0 }}>
                  <div>
                    <MappingCameraPreview active={cameraStatus.state !== "disconnected" && cameraStatus.state !== "error"} />
                  </div>
                </div>
              </div>
            </div>
            <div className="viewer-metrics"><span>Map {formatCount(selectedMap?.point_count)} pts</span><span>Visible {formatCount(viewerMetrics?.visiblePoints)} pts</span><span>Tiles {viewerMetrics?.loadedTiles ?? 0}</span><span>{viewerMetrics?.frameTimeMs.toFixed(1) ?? "—"} ms</span></div>
          </Card>
          <Card title="Frame browser" eyebrow="INCREMENTAL QUALITY" actions={<span className="muted">Click frame to toggle · Click × to delete</span>}>
            {loading ? <Skeleton lines={3} /> : selectedSet?.frames?.length ? <div className="frame-browser">{selectedSet.frames.map((frame) => <button key={frame.id} className={`frame-thumb ${!frame.included ? "is-excluded" : ""}`} onClick={() => void toggleFrame(frame)} title={frame.included ? "Exclude frame" : "Restore frame"}>{frame.thumbnail_url ? <img src={frame.thumbnail_url} alt={`Capture frame ${frame.sequence}`} /> : <span className="frame-thumb__placeholder">{String(frame.sequence).padStart(3, "0")}</span>}<span className={`quality-dot quality-dot--${frame.quality ?? "good"}`} /><small>{frame.blur_score == null ? "pending" : `blur ${frame.blur_score.toFixed(0)}`}</small><span className="frame-delete" onClick={(e) => void deleteFrame(e, frame)} title="Delete frame">×</span></button>)}</div> : <p className="muted">No frames yet. Use Record3D replay/capture or import colour images with matching intrinsics.</p>}
          </Card>
        </div>
        <aside className="workflow-sidebar">
          <Card title="Capture quality" eyebrow="ADMISSION">
            <div className="metric-grid"><Metric label="Captured" value={formatCount(selectedSet?.frame_count)} /><Metric label="Accepted" value={formatCount(acceptedFrames)} tone={acceptedFrames >= 7 ? "good" : "warning"} /><Metric label="Excluded" value={formatCount(selectedSet?.excluded_frame_count)} /><Metric label="Coverage" value={selectedSet?.coverage == null ? "—" : `${(selectedSet.coverage * 100).toFixed(0)}%`} /></div>
            {acceptedFrames < 7 ? <InlineAlert tone="warning" title={`${7 - acceptedFrames} more accepted frames required`}>Map creation requires at least 7; 15 or more with varied viewpoints is recommended.</InlineAlert> : acceptedFrames < 15 ? <InlineAlert tone="warning" title="Usable but lightly covered">You can build now; another {15 - acceptedFrames} varied frames are recommended.</InlineAlert> : <InlineAlert tone="success" title="Frame count ready">Review blur, exposure and coverage before reconstruction.</InlineAlert>}
          </Card>
          <Card title="Build reference map" eyebrow="DURABLE JOB">
            <Field label="Map name"><TextInput value={mapName} maxLength={80} onChange={(event) => setMapName(event.target.value)} /></Field>
            <Field label="Compute profile" hint={computeProfile === "auto" ? `Will use ${compute}` : computeProfile === "cpu" ? "Portable SIFT profile; slower but reproducible." : "Requires verified CUDA and model assets."}><select className="select" value={computeProfile} onChange={(event) => setComputeProfile(event.target.value)}><option value="auto">Auto (verified)</option><option value="cpu">CPU · SIFT</option><option value="cuda">CUDA · ALIKED + LightGlue</option></select></Field>
            <Button variant="primary" busy={busy} disabled={!canBuild} onClick={() => void buildMap()}>Build point-cloud map</Button>
            <p className="muted">The existing active map stays unchanged until a validated new map is explicitly activated.</p>
          </Card>
          {activeJob ? <JobCard job={activeJob} onRefresh={() => void refresh()} /> : null}
          {selectedMap ? <Card title="Map result" eyebrow="VALIDATION"><div className="metric-grid"><Metric label="Points" value={formatCount(selectedMap.point_count)} /><Metric label="Registered" value={formatCount(selectedMap.registered_image_count)} /><Metric label="Reprojection" value={selectedMap.reprojection_error_px == null ? "—" : `${selectedMap.reprojection_error_px.toFixed(2)} px`} /><Metric label="Size" value={formatBytes(selectedMap.size_bytes)} /></div><StatusBadge state={selectedMap.active ? "active" : selectedMap.state} />
            {selectedMap.effective_compute_profile?.includes("cuda") ? <><SfmBoardAlignment projectId={projectId} mapId={selectedMap.id} mapState={selectedMap.state} boardCalibrationId={selectedMap.aruco_board_calibration_id} busy={busy} setBusy={setBusy} refresh={refresh} onAligned={() => viewerRef.current?.reloadMap?.()} /><OpenMvsRunner projectId={projectId} mapId={selectedMap.id} mapState={selectedMap.state} busy={busy} setBusy={setBusy} refresh={refresh} /></> : null}
          </Card> : null}
        </aside>
      </div>
    </div>
  );
}

function SfmBoardAlignment({ projectId, mapId, mapState, boardCalibrationId, busy, setBusy, refresh, onAligned }: { projectId: string; mapId: string; mapState: string; boardCalibrationId?: string; busy: boolean; setBusy: (busy: boolean) => void; refresh: () => Promise<void>; onAligned: () => void }) {
  const pushToast = useUiStore((state) => state.pushToast);
  const [referenceMarkerId, setReferenceMarkerId] = useState("7");
  const [markerSizeMm, setMarkerSizeMm] = useState("35");
  const [maxRmsPx, setMaxRmsPx] = useState("2.0");
  const canRun = mapState.startsWith("ready") || mapState === "active";

  const alignWithReference = async () => {
    const ids = Array.from(new Set(referenceMarkerId.split(",").map((v) => Number.parseInt(v.trim(), 10)).filter(Number.isFinite)));
    const markerSizeM = Number(markerSizeMm) / 1000;
    const threshold = Number(maxRmsPx);
    if (!ids.length || !Number.isFinite(markerSizeM) || markerSizeM <= 0) {
      pushToast({ kind: "error", title: "Invalid settings", message: "Enter a valid reference marker ID (e.g. 7) and a positive marker size in mm." });
      return;
    }
    if (!Number.isFinite(threshold) || threshold <= 0) {
      pushToast({ kind: "error", title: "Invalid settings", message: "Enter a positive maximum RMS reprojection error." });
      return;
    }
    setBusy(true);
    try {
      // 1. Create or update reference marker calibration (single marker or reference anchor)
      const calib = await api.maps.createArucoBoard(projectId, mapId, ids, markerSizeM, 0, ids.length);
      const calibId = calib.board_calibration.board_calibration_id || boardCalibrationId;
      if (!calibId) throw new Error("Failed to create reference marker calibration");

      // 2. Align map using the reference marker
      const map = await api.maps.alignAruco(projectId, mapId, calibId, ids, threshold);
      const metrics = map.similarity_s_w_m0;
      onAligned();
      await refresh();
      pushToast({
        kind: "success",
        title: "SfM map aligned to reference marker",
        message: metrics ? `${metrics.inlier_view_count}/${metrics.view_count} views aligned; RMS ${metrics.rms_reprojection_error_px.toFixed(2)} px. All scene markers triangulated.` : "Reference alignment completed.",
      });
    } catch (error) {
      pushToast({ kind: "error", title: "Alignment failed", message: errorMessage(error) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="button-row" style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
      <Field label="Reference ArUco marker alignment" hint="Establishes origin and metric scale from a reference marker of known size. All scene markers are automatically triangulated in 3D at their exact physical locations.">
        <div className="button-row">
          <TextInput aria-label="Reference marker ID" placeholder="Reference Marker ID (e.g. 7)" value={referenceMarkerId} onChange={(e) => setReferenceMarkerId(e.target.value)} />
          <TextInput aria-label="Marker side length in millimetres" placeholder="Marker side length (mm)" value={markerSizeMm} onChange={(e) => setMarkerSizeMm(e.target.value)} />
        </div>
      </Field>
      <Field label="Accept RMS reprojection error (px)" hint="Alignment is accepted when robust corner reprojection RMS is at or below this threshold.">
        <TextInput aria-label="Maximum RMS reprojection error" value={maxRmsPx} onChange={(e) => setMaxRmsPx(e.target.value)} />
      </Field>
      <div className="button-row">
        <Button size="sm" variant="primary" busy={busy} disabled={!canRun} onClick={() => void alignWithReference()}>
          Align SfM map to ArUco reference marker
        </Button>
        {boardCalibrationId ? (
          <Button size="sm" onClick={() => api.maps.downloadArucoBoard(projectId, boardCalibrationId)}>
            Download calibration JSON
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function OpenMvsRunner({ projectId, mapId, mapState, busy, setBusy, refresh }: { projectId: string, mapId: string, mapState: string, busy: boolean, setBusy: (busy: boolean) => void, refresh: () => Promise<void> }) {
  const pushToast = useUiStore((state) => state.pushToast);
  const upsertJob = useJobStore((state) => state.upsert);
  const [binPath, setBinPath] = useState(localStorage.getItem("spa_openmvs_bin") ?? "");
  const [meshJob, setMeshJob] = useState<JobSnapshot | null>(null);
  const canRun = mapState.startsWith("ready") || mapState === "active";

  useEffect(() => {
    if (!meshJob?.id || ["completed", "cancelled", "failed"].includes(meshJob.state)) return;
    const timer = window.setInterval(async () => {
      try {
        const updated = await api.jobs.get(meshJob.id);
        setMeshJob(updated);
        upsertJob(updated);
        if (updated.state === "completed") {
          pushToast({ kind: "success", title: "OpenMVS mesh generated successfully!" });
          await refresh();
        } else if (updated.state === "failed") {
          pushToast({ kind: "error", title: "OpenMVS mesh generation failed", message: updated.error?.message });
        }
      } catch {}
    }, 1000);
    return () => window.clearInterval(timer);
  }, [meshJob?.id, meshJob?.state, refresh, upsertJob]);

  const startMeshGeneration = async () => {
    setBusy(true);
    try {
      const res = await api.maps.generateMesh(projectId, mapId, binPath);
      const initialJob: JobSnapshot = {
        id: res.job_id,
        owner_project_id: projectId,
        owner_id: mapId,
        type: "mesh",
        state: "queued",
        stage: "interface_colmap",
        stage_index: 1,
        stage_count: 4,
        progress: 0.05,
        message: "Queueing OpenMVS pipeline…",
        created_at: new Date().toISOString(),
      };
      setMeshJob(initialJob);
      upsertJob(initialJob);
      pushToast({ kind: "success", title: "Mesh generation queued" });
      await refresh();
    } catch (error) {
      pushToast({ kind: "error", title: "Action failed", message: errorMessage(error) });
    } finally {
      setBusy(false);
    }
  };

  const cancelJob = async () => {
    if (!meshJob?.id) return;
    setBusy(true);
    try {
      const cancelled = await api.jobs.cancel(meshJob.id);
      setMeshJob(cancelled);
      upsertJob(cancelled);
      pushToast({ kind: "warning", title: "Mesh generation cancelled" });
    } catch (err) {
      pushToast({ kind: "error", title: "Cancel failed", message: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };

  const isRunning = meshJob && ["queued", "admitted", "processing"].includes(meshJob.state);

  const stages = [
    { key: "interface_colmap", label: "1. Convert", index: 1 },
    { key: "densify_point_cloud", label: "2. Densify", index: 2 },
    { key: "reconstruct_mesh", label: "3. Mesh", index: 3 },
    { key: "texture_mesh", label: "4. Texture", index: 4 },
  ];

  return (
    <div className="button-row" style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 10 }}>
      <Field label="OpenMVS Bin Path (Optional)" hint="e.g. C:\OpenMVS\bin (or leave empty if in PATH)">
        <TextInput
          value={binPath}
          placeholder="C:\OpenMVS\bin (optional)"
          disabled={Boolean(isRunning)}
          onChange={(e) => {
            setBinPath(e.target.value);
            localStorage.setItem("spa_openmvs_bin", e.target.value);
          }}
        />
      </Field>
      <Button
        size="sm"
        variant="primary"
        busy={busy || Boolean(isRunning)}
        disabled={!canRun || Boolean(isRunning)}
        onClick={() => void startMeshGeneration()}
      >
        {isRunning ? "Generating textured mesh…" : "Generate textured mesh (OpenMVS)"}
      </Button>

      {meshJob ? (
        <div style={{ padding: "0.75rem", background: "rgba(10, 18, 26, 0.9)", border: "1px solid var(--line-strong)", borderRadius: "8px", display: "flex", flexDirection: "column", gap: "8px", marginTop: "2px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <strong style={{ fontSize: "0.8rem", color: "var(--cyan)" }}>OpenMVS Status</strong>
            <StatusBadge state={meshJob.state} />
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "4px", margin: "2px 0" }}>
            {stages.map((st) => {
              const isCurrent = meshJob.stage === st.key && isRunning;
              const isPast = (meshJob.stage_index ?? 0) > st.index || meshJob.state === "completed";
              return (
                <div
                  key={st.key}
                  style={{
                    padding: "4px 2px",
                    textAlign: "center",
                    fontSize: "0.62rem",
                    borderRadius: "4px",
                    fontWeight: 600,
                    background: isCurrent ? "rgba(46, 146, 174, 0.35)" : isPast ? "rgba(39, 134, 106, 0.25)" : "rgba(255, 255, 255, 0.04)",
                    color: isCurrent ? "var(--cyan)" : isPast ? "var(--green)" : "var(--muted)",
                    border: `1px solid ${isCurrent ? "var(--cyan)" : isPast ? "#27866a" : "rgba(255, 255, 255, 0.08)"}`,
                  }}
                >
                  {isPast && meshJob.state !== "processing" ? "✓ " : ""}{st.label}
                </div>
              );
            })}
          </div>

          <ProgressBar value={meshJob.state === "completed" ? 1 : Math.max(0.05, meshJob.progress ?? 0)} />

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.72rem", color: "var(--text-soft)" }}>
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "80%" }}>
              {meshJob.message || (meshJob.stage ? `Processing ${meshJob.stage.replaceAll("_", " ")}…` : "Working…")}
            </span>
            <strong style={{ fontVariantNumeric: "tabular-nums" }}>{Math.round((meshJob.progress ?? 0) * 100)}%</strong>
          </div>

          {meshJob.state === "failed" && (
            <InlineAlert tone="danger" title="OpenMVS Failed">
              {meshJob.error?.message || "Mesh generation encountered an error."}
            </InlineAlert>
          )}

          {meshJob.state === "completed" && (
            <InlineAlert tone="success" title="Mesh Ready">
              Full-color textured mesh ready. Switch View mode to <strong>Mesh</strong> to inspect!
            </InlineAlert>
          )}

          {isRunning && (
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "2px" }}>
              <Button size="sm" variant="danger" busy={busy} onClick={() => void cancelJob()}>
                Cancel job
              </Button>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

function MappingCameraPreview({ active }: { active: boolean }) {
  const [rgbUrl, setRgbUrl] = useState<string | null>(null);
  const [state, setState] = useState("closed");
  const urls = useRef<string[]>([]);

  useEffect(() => {
    if (!active) { setState("closed"); return; }
    const latest = new LatestFrameBuffer<BinaryStreamMessage>();
    let renderFrame = 0;
    const stream = new ReconnectingSocket("/ws/v1/camera/preview", {
      onState: setState,
      onBinary: (message) => latest.push(message),
    });
    stream.connect();
    stream.send("subscribe", { channels: ["rgb"], quality: "low" });
    const consume = () => {
      if (!document.hidden) {
        const message = latest.take();
        if (message) {
          const header = message.header;
          const encoding = String(header.encoding ?? "jpeg");
          const mime = encoding.includes("jpeg") || encoding.includes("jpg") ? "image/jpeg" : "image/png";
          const url = URL.createObjectURL(new Blob([message.payload], { type: mime }));
          urls.current.push(url);
          setRgbUrl((previous) => { if (previous) URL.revokeObjectURL(previous); return url; });
        }
      }
      renderFrame = requestAnimationFrame(consume);
    };
    renderFrame = requestAnimationFrame(consume);
    return () => {
      cancelAnimationFrame(renderFrame);
      stream.close();
      urls.current.forEach(URL.revokeObjectURL);
      urls.current = [];
      setRgbUrl(null);
    };
  }, [active]);

  if (!active) {
    return (
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--muted)", fontSize: "0.75rem", padding: "1rem", textAlign: "center" }}>
        <span style={{ fontSize: "1.5rem", marginBottom: "0.4rem", color: "var(--cyan)" }}>▣</span>
        <strong>Camera disconnected</strong>
        <small style={{ marginTop: "0.2rem" }}>Connect Record3D to view live preview</small>
      </div>
    );
  }

  return (
    <div style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center", background: "#05080b" }}>
      {rgbUrl ? (
        <img
          src={rgbUrl}
          alt="Live camera RGB preview"
          style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
          onLoad={(e) => {
            const img = e.currentTarget;
            if (img.naturalWidth && img.naturalHeight) {
              const previewBox = img.closest<HTMLElement>(".tracking-feed-preview");
              if (previewBox) {
                previewBox.style.setProperty("aspect-ratio", `${img.naturalWidth} / ${img.naturalHeight}`);
              }
            }
          }}
        />
      ) : (
        <span style={{ display: "flex", alignItems: "center", gap: "6px", color: "var(--muted)", fontSize: "0.75rem" }}>
          <span className="spinner" /> Waiting for RGB frame…
        </span>
      )}
    </div>
  );
}

function JobCard({ job, onRefresh }: { job: JobSnapshot; onRefresh: () => void }) {
  const pushToast = useUiStore((state) => state.pushToast);
  const [busy, setBusy] = useState(false);
  const act = async (action: "cancel" | "resume") => {
    setBusy(true);
    try { action === "cancel" ? await api.jobs.cancel(job.id) : await api.jobs.resume(job.id); pushToast({ kind: "success", title: action === "cancel" ? "Cancellation requested" : "Job resumed" }); onRefresh(); }
    catch (value) { pushToast({ kind: "error", title: "Job action failed", message: errorMessage(value) }); }
    finally { setBusy(false); }
  };
  return <Card title="Reconstruction progress" eyebrow={job.effective_compute_profile ?? "COMPUTE"} actions={<StatusBadge state={job.state} />}><div className="job-stage"><strong>{job.stage?.replaceAll("_", " ") ?? job.message ?? "Waiting for resources"}</strong><span>{Math.round((job.progress ?? 0) * 100)}%</span></div><ProgressBar value={job.progress} />{job.stage_count ? <p className="muted">Stage {job.stage_index ?? 0} of {job.stage_count} · {job.message}</p> : null}{job.warnings?.map((warning) => <InlineAlert key={warning} tone="warning" title="Processing warning">{warning}</InlineAlert>)}<div className="button-row">{["queued", "admitted", "processing"].includes(job.state) ? <Button variant="danger" size="sm" busy={busy} onClick={() => void act("cancel")}>Cancel safely</Button> : null}{job.state === "recoverable" ? <Button variant="primary" size="sm" busy={busy} onClick={() => void act("resume")}>Resume checkpoint</Button> : null}</div></Card>;
}
