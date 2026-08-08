import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import { probeWorkflowApi, type ProbeCapture } from "../api/probeWorkflows";
import { parseBinaryMessage, ReconnectingSocket } from "../api/streams";
import type { CalibrationValidation, ProbeCalibration, ProbeTestMetrics, Registration, TrackingViewFrame } from "../api/types";
import { BlobDetectorTuningModal } from "../features/probe/BlobDetectorTuningModal";
import { SpatialViewer } from "../viewer/react/SpatialViewer";
import { Button, Card, EmptyState, Field, InlineAlert, Metric, Modal, ProgressBar, Segmented, Skeleton, StatusBadge, TextInput } from "../components/ui";
import { useCameraStore, useProjectStore, useUiStore } from "../stores";
import { formatCount, formatDate } from "../utils/format";

export function ProbeRegistrationPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const project = useProjectStore((state) => state.activeProject);
  const activeMap = useProjectStore((state) => state.activeMap);
  const cameraReady = useCameraStore((state) => state.status.state === "ready");
  const pushToast = useUiStore((state) => state.pushToast);
  const [calibrations, setCalibrations] = useState<ProbeCalibration[]>([]);
  const [registrations, setRegistrations] = useState<Registration[]>([]);
  const [selectedCalibrationId, setSelectedCalibrationId] = useState(project?.active_probe_calibration_id ?? "");
  const [selectedRegistrationId, setSelectedRegistrationId] = useState(project?.active_registration_id ?? "");
  const [probeMetrics, setProbeMetrics] = useState<ProbeTestMetrics>({ blob_count: 0, candidate_count: 0, inliers: 0, tracked: false });
  const [testState, setTestState] = useState("closed");
  const [tuningOpen, setTuningOpen] = useState(false);
  const [capture, setCapture] = useState<ProbeCapture | null>(null);
  const [calibrationName, setCalibrationName] = useState("Five-marker probe calibration");
  const [validation, setValidation] = useState<CalibrationValidation | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [overlayUrl, setOverlayUrl] = useState<string | null>(null);
  const overlayObjectUrls = useRef<string[]>([]);
  const [observationMode, setObservationMode] = useState<"aruco" | "kinematic">(() => {
    return (localStorage.getItem("spa_registration_observation_mode") as "aruco" | "kinematic") || "aruco";
  });
  const [workflowMode, setWorkflowMode] = useState<"standard" | "aruco_joint">(() => {
    return (localStorage.getItem("spa_workflow_mode") as "standard" | "aruco_joint") || "standard";
  });
  const [arucoIdsStr, setArucoIdsStr] = useState("6,7,5");
  const [kinematicPose, setKinematicPose] = useState<{ camera_pose_w: number[]; probe_pose_c: number[] } | null>(null);
  const [cameraState, setCameraState] = useState<"tracked" | "lost">("lost");
  const [cameraInliers, setCameraInliers] = useState<number>(0);

  const handleObservationModeChange = (mode: "aruco" | "kinematic") => {
    setObservationMode(mode);
    localStorage.setItem("spa_registration_observation_mode", mode);
  };
  const handleWorkflowModeChange = (mode: "standard" | "aruco_joint") => {
    setWorkflowMode(mode);
    localStorage.setItem("spa_workflow_mode", mode);
  };

  const refresh = async (signal?: AbortSignal) => {
    setLoading(true); setError(null);
    try {
      const [calibrationValues, registrationValues] = await Promise.all([api.probe.list(projectId, signal), api.registration.list(projectId, signal)]);
      setCalibrations(calibrationValues); setRegistrations(registrationValues);
      setSelectedCalibrationId((current) => current || calibrationValues.find((item) => item.active)?.id || calibrationValues[0]?.id || "");
      setSelectedRegistrationId((current) => current || registrationValues.find((item) => item.active)?.id || registrationValues[0]?.id || "");
    } catch (value) { if (!signal?.aborted) setError(errorMessage(value)); }
    finally { if (!signal?.aborted) setLoading(false); }
  };
  useEffect(() => { const controller = new AbortController(); void refresh(controller.signal); return () => controller.abort(); }, [projectId]);
  useEffect(() => {
    if (!cameraReady) { setTestState("closed"); return; }
    const stream = new ReconnectingSocket(`/ws/v1/projects/${projectId}/probe-test`, {
      onState: setTestState,
      onEnvelope: (envelope) => { if (envelope.type === "probe.tracking_test") setProbeMetrics(envelope.data as ProbeTestMetrics); },
    });
    stream.connect();
    if (selectedCalibrationId) {
      stream.send("subscribe", { calibration_id: selectedCalibrationId });
    }
    return () => stream.close();
  }, [cameraReady, projectId, selectedCalibrationId]);
  const viewerRef = useRef<import("../viewer/react/SpatialViewerV1").SpatialViewerHandle>(null);

  useEffect(() => {
    if (!cameraReady || tuningOpen) {
      setOverlayUrl(null);
      return;
    }
    const stream = new ReconnectingSocket(`/ws/v1/projects/${projectId}/probe-tuning`, {
      onBinary: (message) => {
        const kind = String(message.header.kind ?? "");
        if (kind !== "overlay") return;
        const url = URL.createObjectURL(new Blob([message.payload], { type: "image/png" }));
        overlayObjectUrls.current.push(url);
        setOverlayUrl((prev) => { if (prev) URL.revokeObjectURL(prev); return url; });
      },
    });
    stream.connect();
    if (selectedCalibrationId) {
      stream.send("subscribe", { calibration_id: selectedCalibrationId });
    }
    if (workflowMode === "aruco_joint") {
      const parsedIds = arucoIdsStr.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n));
      stream.send("tuning.patch", { marker_ids: parsedIds });
    }
    return () => {
      stream.close();
      overlayObjectUrls.current.forEach(URL.revokeObjectURL);
      overlayObjectUrls.current = [];
      setOverlayUrl(null);
    };
  }, [cameraReady, projectId, selectedCalibrationId, tuningOpen]);

  useEffect(() => {
    if (!cameraReady || !selectedCalibrationId) {
      setKinematicPose(null);
      return;
    }
    if (observationMode === "kinematic" && !activeMap && workflowMode !== "aruco_joint") {
      setKinematicPose(null);
      return;
    }
    const targetRegId = selectedRegistrationId || "active";
    const stream = new ReconnectingSocket(`/ws/v1/projects/${projectId}/registrations/${targetRegId}/tracking`, {
      onEnvelope: (envelope) => {
        if (envelope.type === "tracking.frame" && envelope.data) {
          const frameData = envelope.data as { t_w_c?: number[]; t_c_m?: number[]; camera_state?: "tracked" | "lost"; camera_inliers?: number } & TrackingViewFrame;
          viewerRef.current?.applyTrackingFrame(frameData);
          setCameraState(frameData.camera_state ?? (frameData.t_w_c ? "tracked" : "lost"));
          setCameraInliers(frameData.camera_inliers ?? 0);
          if (frameData.t_w_c && frameData.t_c_m) {
            setKinematicPose({ camera_pose_w: frameData.t_w_c, probe_pose_c: frameData.t_c_m });
          } else {
            setKinematicPose(null);
          }
        }
      },
    });
    stream.connect();
    return () => stream.close();
  }, [cameraReady, projectId, selectedRegistrationId, observationMode, activeMap, selectedCalibrationId, workflowMode]);

  useEffect(() => {
    if (workflowMode === "aruco_joint" && selectedCalibrationId) {
      const correspondingReg = registrations.find(r => r.probe_calibration_id === selectedCalibrationId);
      if (correspondingReg && correspondingReg.id !== selectedRegistrationId) {
        setSelectedRegistrationId(correspondingReg.id);
      }
    }
  }, [workflowMode, selectedCalibrationId, registrations, selectedRegistrationId]);

  const selectedCalibration = calibrations.find((item) => item.id === selectedCalibrationId);
  const selectedRegistration = registrations.find((item) => item.id === selectedRegistrationId);
  
  const fallbackBoardDefinition = useMemo(() => {
    const ids = arucoIdsStr.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n));
    const validIds = ids.length > 0 ? ids : [6, 7, 5];
    const layout: Record<string, number[][]> = {};
    const markerSize = 0.035;
    const spacing = 0.07;
    validIds.forEach((id, index) => {
      const cx = (index - (validIds.length - 1) / 2) * spacing;
      layout[String(id)] = [
        [cx - markerSize / 2, markerSize / 2, 0], // Top-Left
        [cx + markerSize / 2, markerSize / 2, 0], // Top-Right
        [cx + markerSize / 2, -markerSize / 2, 0], // Bottom-Right
        [cx - markerSize / 2, -markerSize / 2, 0], // Bottom-Left
      ];
    });
    return {
      dictionary: "DICT_4X4_50",
      marker_ids: validIds,
      anchor_id: validIds[1] ?? validIds[0],
      marker_size_m: markerSize,
      layout,
    };
  }, [arucoIdsStr]);

  const registrationView = useMemo(() => ({
    t_w_b: selectedRegistration?.t_w_b, 
    scale: selectedRegistration?.scale, 
    board_definition: selectedRegistration?.board_definition ?? (workflowMode === "aruco_joint" || observationMode === "aruco" ? fallbackBoardDefinition : undefined)
  }), [selectedRegistration?.t_w_b, selectedRegistration?.scale, selectedRegistration?.board_definition, workflowMode, observationMode, fallbackBoardDefinition]);

  const isValidated = ["passed", "accepted_with_warning", "failed"].includes(selectedRegistration?.validation_state ?? "") || selectedRegistration?.state === "validated";
  const registrationStep = !selectedRegistration
    ? 0
    : (selectedRegistration.observation_count ?? 0) < 3
    ? 1
    : !selectedRegistration.scale || selectedRegistration.state === "draft"
    ? 2
    : !isValidated
    ? 3
    : selectedRegistration.active
    ? 5
    : 4;
  const readyForLive = Boolean(selectedCalibration?.active && selectedRegistration?.active && ["passed", "accepted_with_warning"].includes(selectedRegistration.validation_state ?? ""));

  const activateCalibration = async (calibration: ProbeCalibration) => {
    setBusy(true);
    try { await api.probe.activate(projectId, calibration.id); setCalibrations((items) => items.map((item) => ({ ...item, active: item.id === calibration.id }))); pushToast({ kind: "success", title: "Probe calibration activated" }); }
    catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const validateImport = async (file: File) => {
    setBusy(true); setValidation(null);
    try { setValidation(await api.probe.validate(projectId, file)); setImportOpen(true); }
    catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const importCalibration = async () => {
    if (!validation?.valid) return;
    setBusy(true);
    try {
      const imported = await api.probe.import(projectId, validation.validation_id, true);
      setCalibrations((items) => [{ ...imported, active: true }, ...items.map((item) => ({ ...item, active: false }))]); setSelectedCalibrationId(imported.id); setImportOpen(false); setValidation(null);
      pushToast({ kind: "success", title: "Calibration imported and activated" });
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const captureCalibrationFrame = async () => {
    setBusy(true);
    try {
      const current = capture ?? await probeWorkflowApi.createCapture(projectId);
      const prevAccepted = capture?.accepted_frame_count ?? 0;
      const updated = await probeWorkflowApi.captureFrame(projectId, current.id, selectedCalibrationId);
      setCapture(updated);
      const newAccepted = updated.accepted_frame_count ?? 0;
      const totalFrames = updated.input_frame_count ?? updated.frame_count ?? 0;
      if (newAccepted > prevAccepted) {
        pushToast({
          kind: "success",
          title: "View captured & accepted",
          message: `${newAccepted}/15 accepted views (${totalFrames} total captured)`,
        });
      } else {
        pushToast({
          kind: "warning",
          title: "View captured, but probe was not tracked",
          message: `Frame saved, but 5 probe markers were not detected. Keep all 5 markers in view (${newAccepted} accepted / ${totalFrames} total captured).`,
        });
      }
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const createCalibration = async (acceptWarning = false) => {
    if (!capture || capture.accepted_frame_count < 3) return;
    setBusy(true);
    try {
      const created = await probeWorkflowApi.createCalibration(projectId, capture.id, calibrationName, acceptWarning);
      setCalibrations((items) => [created, ...items]);
      setSelectedCalibrationId(created.id);
      setCapture(null);
      pushToast({ kind: "success", title: "Probe calibration created successfully" });
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const createRegistration = async () => {
    if (!activeMap || !selectedCalibration) return;
    setBusy(true);
    try { const value = await api.registration.create(projectId, activeMap.id, selectedCalibration.id); setRegistrations((items) => [value, ...items]); setSelectedRegistrationId(value.id); }
    catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  
  const captureArucoFrame = async () => {
    setBusy(true);
    try {
      const parsedIds = arucoIdsStr.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n));
      const current = capture ?? (await probeWorkflowApi.arucoCapture(projectId, undefined, parsedIds)).probe_capture;
      const response = await probeWorkflowApi.arucoCapture(projectId, current.id, parsedIds);
      setCapture(response.probe_capture);
      pushToast({ kind: "success", title: "Joint view captured", message: `${response.probe_capture.accepted_frame_count}/3 minimum accepted` });
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };

  const solveArucoCalibration = async () => {
    if (!capture || capture.accepted_frame_count < 3) return;
    setBusy(true);
    try {
      const parsedIds = arucoIdsStr.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n));
      const anchorId = parsedIds[1] ?? 7; // Use second ID as anchor or default to 7
      const result = await probeWorkflowApi.arucoSolve(projectId, capture.id, parsedIds, anchorId);
      setCalibrations((items) => [result.probe_calibration, ...items]);
      setSelectedCalibrationId(result.probe_calibration.id);
      setRegistrations((items) => [result.registration, ...items]);
      setSelectedRegistrationId(result.registration.id);
      setCapture(null);
      pushToast({ kind: "success", title: "ArUco Joint Calibration complete" });
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const registrationAction = async (action: "observe" | "solve" | "validate" | "accept" | "activate") => {
    if (!selectedRegistration) return;
    if (action === "observe" && observationMode === "kinematic" && !kinematicPose) {
      setError("Kinematic observation requires both Camera PnP (mapped scene) and 5-marker probe (5/5) to be tracked simultaneously.");
      return;
    }
    setBusy(true);
    try {
      const payload = observationMode === "kinematic" && kinematicPose ? { camera_pose_w: kinematicPose.camera_pose_w, probe_pose_c: kinematicPose.probe_pose_c, label: `kinematic_${(selectedRegistration.observation_count ?? 0) + 1}` } : undefined;
      const value = action === "observe" ? await api.registration.addObservation(projectId, selectedRegistration.id, payload)
        : action === "solve" ? await api.registration.solve(projectId, selectedRegistration.id)
          : action === "validate" || action === "accept" ? await api.registration.validate(projectId, selectedRegistration.id, action === "accept")
            : await api.registration.activate(projectId, selectedRegistration.id);
      setRegistrations((items) => items.map((item) => item.id === value.id ? value : action === "activate" ? { ...item, active: false } : item));
      pushToast({ kind: "success", title: action === "observe" ? "Observation captured" : `Registration ${action}d` });
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };


  const clearObservations = async () => {
    if (!selectedRegistration) return;
    setBusy(true);
    try {
      const updated = await api.registration.clearObservations(projectId, selectedRegistration.id);
      setRegistrations((items) => items.map((item) => item.id === updated.id ? updated : item));
      pushToast({ kind: "success", title: "Registration observations cleared" });
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };

  if (loading) return <div className="page"><Skeleton lines={8} /></div>;
  return (
    <div className="page page--workflow">
      <header className="page-heading">
        <div>
          <div className="eyebrow">STEP 3 · METRIC ALIGNMENT</div>
          <h1>Probe & Registration</h1>
          <p>Validate reusable five-marker probe geometry, then solve board/tissue scale and map alignment.</p>
        </div>
        <div className="page-heading__actions">
          <Segmented
            label="Workflow mode"
            value={workflowMode}
            options={[{ value: "standard", label: "Standard Map Mode" }, { value: "aruco_joint", label: "ArUco Board Mode" }]}
            onChange={handleWorkflowModeChange}
          />
          {readyForLive ? <Button variant="primary" onClick={() => navigate(`/projects/${projectId}/live`)}>Continue to live painting →</Button> : null}
        </div>
      </header>
      {!activeMap && workflowMode !== "aruco_joint" ? <InlineAlert tone="warning" title="An active map is required" action={<Button size="sm" onClick={() => navigate(`/projects/${projectId}/mapping`)}>Open mapping</Button>}>Create, inspect and activate a map before registration. Calibration import remains available.</InlineAlert> : null}
      {error ? (
        <InlineAlert
          tone="danger"
          title="Probe or registration action failed"
          action={
            <div className="button-row">
              {error.includes("2.5 px") || error.includes("threshold") ? (
                <Button size="sm" variant="primary" onClick={() => { setError(null); void createCalibration(true); }}>
                  Accept warning & create
                </Button>
              ) : null}
              <Button size="sm" onClick={() => setError(null)}>Dismiss</Button>
            </div>
          }
        >
          {error}
        </InlineAlert>
      ) : null}
      <div className="status-card-row">
        <Metric label="Active calibration" value={selectedCalibration?.active ? `r${selectedCalibration.revision}` : "Not active"} tone={selectedCalibration?.active ? "good" : "warning"} />
        <Metric label="Camera PnP" value={observationMode === "kinematic" ? (cameraState === "tracked" ? "Tracked" : "Lost") : "N/A"} tone={observationMode === "kinematic" && cameraState === "tracked" ? "good" : observationMode === "kinematic" ? "warning" : undefined} detail={observationMode === "kinematic" ? `${cameraInliers} map inliers` : "ArUco mode"} />
        <Metric label="Probe tracking" value={probeMetrics.tracked ? "5 / 5" : `${probeMetrics.inliers} / 5`} tone={probeMetrics.tracked ? "good" : "warning"} detail={testState === "open" ? "live" : testState} />
        <Metric label="Calibration error" value={selectedCalibration ? `${selectedCalibration.quality.rms_reprojection_error_px.toFixed(2)} px` : "—"} />
        <Metric label="Registration RMS" value={selectedRegistration?.rms_residual_mm == null ? "—" : `${selectedRegistration.rms_residual_mm.toFixed(2)} mm`} tone={(selectedRegistration?.rms_residual_mm ?? 0) > 3 ? "warning" : undefined} />
        <Metric label="Scale" value={selectedRegistration?.scale == null ? "—" : `${selectedRegistration.scale.toFixed(6)}×`} />
      </div>
      <div className="workflow-grid workflow-grid--registration">
        <div className="registration-main">
          <Card className="viewer-card registration-workspace-card" title="Registration workspace" eyebrow={workflowMode === "aruco_joint" ? "ARUCO BOARD" : (activeMap?.name ?? "MAP REQUIRED")} actions={<StatusBadge state={selectedRegistration?.validation_state ?? "pending"} />}>
            <div className="registration-workspace-layout">
              <div className="registration-viewer-wrap">
                {activeMap || workflowMode === "aruco_joint" ? <SpatialViewer ref={viewerRef} mode="registration" projectId={projectId} mapId={activeMap?.id ?? ""} registration={registrationView} probeGeometry={selectedCalibration?.probe?.marker_points_m} /> : <div className="viewer-empty"><EmptyState icon="⌖" title="No active reference map">Return to Mapping and activate a validated point cloud.</EmptyState></div>}
              </div>
              <div className="registration-record3d-widget">
                <div className="record3d-widget-header">
                  <div>
                    <div className="eyebrow">LIVE RECORD3D</div>
                    <strong>Probe tracking test</strong>
                  </div>
                  <StatusBadge state={testState} />
                </div>
                <div className="tracking-feed-preview">
                  <div>
                    {overlayUrl ? (
                      <img
                        src={overlayUrl}
                        alt="Detected overlay"
                        onLoad={(e) => {
                          const img = e.currentTarget;
                          if (img.naturalWidth && img.naturalHeight) {
                            img.parentElement?.parentElement?.style.setProperty("aspect-ratio", `${img.naturalWidth} / ${img.naturalHeight}`);
                          }
                        }}
                      />
                    ) : (
                      <span><span className="spinner" /> {cameraReady ? "Waiting for frame…" : "Waiting for camera"}</span>
                    )}
                  </div>
                </div>
                <div className="probe-test-visual">
                  <div className="probe-dot-pattern">{Array.from({ length: 5 }).map((_, index) => <span key={index} className={index < probeMetrics.inliers ? "is-found" : ""} />)}</div>
                  <strong>{probeMetrics.tracked ? "5/5 tracked" : "Probe not fully tracked"}</strong>
                  <small>{probeMetrics.rejection_reason ?? probeMetrics.exposure_feedback ?? (cameraReady ? "Keep all markers in view." : "Connect Record3D or replay to test.")}</small>
                </div>
                {observationMode === "kinematic" ? (
                  <div style={{ padding: ".6rem", background: "rgba(255,255,255,0.04)", borderRadius: "6px", border: "1px solid rgba(255,255,255,0.1)", margin: ".4rem 0 .6rem 0" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <strong style={{ fontSize: "0.85rem" }}>Camera Map PnP:</strong>
                      <StatusBadge state={cameraState === "tracked" ? "passed" : "warning"} />
                    </div>
                    <small style={{ color: "#94a3b8", fontSize: "0.75rem", display: "block", marginTop: ".2rem" }}>
                      {cameraState === "tracked" ? `Localizing live camera (${cameraInliers} SIFT inliers)` : "Point camera at mapped scene to localize"}
                    </small>
                  </div>
                ) : null}
                <div className="metric-grid">
                  <Metric label="Blobs" value={probeMetrics.blob_count} />
                  <Metric label="Candidates" value={probeMetrics.candidate_count} />
                  <Metric label="Error" value={probeMetrics.reprojection_error_px == null ? "—" : `${probeMetrics.reprojection_error_px.toFixed(2)} px`} />
                </div>
                <Button variant="primary" disabled={!selectedCalibration || !cameraReady} onClick={() => setTuningOpen(true)}>Can’t track the probe?</Button>
              </div>
            </div>
          </Card>
          {workflowMode === "aruco_joint" ? (
            <Card title="ArUco Joint Calibration Steps" eyebrow="BOARD & PROBE SETUP">
              <ol className="registration-stepper">
                <RegistrationStep 
                  index={1} 
                  title="Configure ArUco Markers" 
                  state={(capture?.accepted_frame_count ?? 0) > 0 ? "complete" : "active"} 
                  detail="Enter the IDs of the markers on your board. The middle ID will be the anchor (default 7)." 
                  action={
                    <Field label="Marker IDs (comma separated)">
                      <TextInput value={arucoIdsStr} onChange={(e) => setArucoIdsStr(e.target.value)} />
                    </Field>
                  } 
                />
                <RegistrationStep 
                  index={2} 
                  title="Capture Joint Views" 
                  state={(capture?.accepted_frame_count ?? 0) >= 3 ? "complete" : "active"} 
                  detail={`${capture?.accepted_frame_count ?? 0}/3 accepted views. Place probe on the board so both are visible.`} 
                  action={<Button size="sm" variant="primary" busy={busy} disabled={!cameraReady} onClick={() => void captureArucoFrame()}>Capture joint view</Button>} 
                />
                <RegistrationStep 
                  index={3} 
                  title="Solve & Activate" 
                  state={selectedRegistration?.active && (selectedRegistration as any)?.is_aruco_mode ? "complete" : (capture?.accepted_frame_count ?? 0) >= 3 ? "active" : "pending"} 
                  detail="Solves probe geometry, board layout, and creates active registration." 
                  action={(capture?.accepted_frame_count ?? 0) >= 3 ? <Button size="sm" variant="primary" busy={busy} onClick={() => void solveArucoCalibration()}>Solve & Activate</Button> : null} 
                />
              </ol>
            </Card>
          ) : (
            <Card
              title="Registration steps"
              eyebrow="BOARD / TISSUE TO MAP"
              actions={
                <Segmented
                  value={observationMode}
                  label="Observation mode"
                  options={[
                    { value: "aruco", label: "ArUco Board" },
                    { value: "kinematic", label: "Kinematic Scale" },
                  ]}
                  onChange={handleObservationModeChange}
                />
              }
            >
              <ol className="registration-stepper">
                <RegistrationStep index={1} title="Create revision" state={registrationStep > 0 ? "complete" : "active"} detail="Locks the exact map and probe calibration revisions." action={registrationStep === 0 ? <Button size="sm" variant="primary" busy={busy} disabled={!activeMap || !selectedCalibration?.active} onClick={() => void createRegistration()}>Create registration</Button> : null} />
                <RegistrationStep
                  index={2}
                  title={observationMode === "aruco" ? "Capture non-degenerate board observations" : "Capture multi-view camera poses"}
                  state={registrationStep > 1 ? "complete" : registrationStep === 1 ? "active" : "pending"}
                  detail={
                    observationMode === "aruco"
                      ? `${selectedRegistration?.observation_count ?? 0} observations; use varied viewpoints and depth.`
                      : `${selectedRegistration?.observation_count ?? 0} kinematic observations; move camera while holding probe stationary.`
                  }
                  action={
                    registrationStep === 1 || registrationStep === 2 ? (
                      <div className="button-row">
                        {registrationStep === 1 ? (
                            <Button size="sm" variant="primary" busy={busy} disabled={!cameraReady || (observationMode === "kinematic" && !kinematicPose)} onClick={() => void registrationAction("observe")}>
                              Capture observation
                            </Button>
                        ) : null}
                        {(selectedRegistration?.observation_count ?? 0) > 0 ? (
                          <Button size="sm" busy={busy} onClick={() => void clearObservations()}>
                            Clear observations
                          </Button>
                        ) : null}
                      </div>
                    ) : null
                  }
                />
                <RegistrationStep index={3} title="Solve physical scale and transforms" state={registrationStep > 2 ? "complete" : registrationStep === 2 ? "active" : "pending"} detail="Requires positive scale and finite, non-degenerate similarity geometry." action={registrationStep === 2 ? <Button size="sm" variant="primary" busy={busy} onClick={() => void registrationAction("solve")}>Solve registration</Button> : null} />
                <RegistrationStep index={4} title="Validate held-out residuals" state={registrationStep > 3 ? "complete" : registrationStep === 3 ? "active" : "pending"} detail={`RMS ${selectedRegistration?.rms_residual_mm?.toFixed(2) ?? "—"} mm · max ${selectedRegistration?.max_residual_mm?.toFixed(2) ?? "—"} mm`} action={registrationStep === 3 ? <Button size="sm" variant="primary" busy={busy} onClick={() => void registrationAction("validate")}>Run validation</Button> : selectedRegistration?.validation_state === "failed" ? <Button size="sm" variant="danger" busy={busy} onClick={() => void registrationAction("accept")}>Accept warning</Button> : null} />
                <RegistrationStep index={5} title="Activate metric registration" state={selectedRegistration?.active ? "complete" : registrationStep >= 4 ? "active" : "pending"} detail="Activation enables Live while preserving prior immutable revisions." action={registrationStep >= 4 && !selectedRegistration?.active ? <Button size="sm" variant="primary" busy={busy} onClick={() => void registrationAction("activate")}>Activate</Button> : null} />
              </ol>
            </Card>
          )}
        </div>
        <aside className="workflow-sidebar">
          <Card title="Calibration library" eyebrow="REUSABLE JSON" actions={<label className="button button--default button--sm file-button">Upload<input type="file" accept=".json,application/json" onChange={(event) => { const file = event.target.files?.[0]; if (file) void validateImport(file); event.target.value = ""; }} /></label>}>
            {calibrations.length ? <div className="calibration-list">{calibrations.map((calibration) => <button className={selectedCalibrationId === calibration.id ? "is-selected" : ""} key={calibration.id} onClick={() => setSelectedCalibrationId(calibration.id)}><span><strong>{calibration.name}</strong><small>r{calibration.revision} · {formatDate(calibration.created_at)} · {calibration.quality.rms_reprojection_error_px.toFixed(2)} px</small></span><StatusBadge state={calibration.active ? "active" : calibration.state} /></button>)}</div> : <p className="muted">Create from images or upload a complete versioned probe_calibration.json.</p>}
            {selectedCalibration ? <div className="button-row"><Button size="sm" onClick={() => api.probe.download(projectId, selectedCalibration.id)}>Download JSON</Button>{!selectedCalibration.active ? <Button size="sm" variant="primary" busy={busy} onClick={() => void activateCalibration(selectedCalibration)}>Activate</Button> : null}</div> : null}
          </Card>
          {workflowMode !== "aruco_joint" ? (
            <Card title="Create probe calibration" eyebrow="3 MIN · 15–25 RECOMMENDED">
              <Field label="Calibration name"><TextInput value={calibrationName} onChange={(event) => setCalibrationName(event.target.value)} /></Field>
              <div className="capture-progress"><div><strong>{capture?.accepted_frame_count ?? 0}</strong><span>accepted ({(capture?.input_frame_count ?? capture?.frame_count ?? 0)} total)</span></div><ProgressBar value={Math.min(1, (capture?.accepted_frame_count ?? 0) / 15)} /></div>
              <div className="button-row"><Button busy={busy} disabled={!cameraReady || !probeMetrics.tracked} onClick={() => void captureCalibrationFrame()}>Capture view</Button><Button variant="primary" busy={busy} disabled={(capture?.accepted_frame_count ?? 0) < 3} onClick={() => void createCalibration()}>Create calibration</Button></div>
              {(capture?.input_frame_count ?? capture?.frame_count ?? 0) > 0 && (capture?.accepted_frame_count ?? 0) === 0 ? (
                <small className="muted" style={{ display: "block", marginTop: ".4rem" }}>
                  {(capture?.input_frame_count ?? capture?.frame_count ?? 0)} frame(s) saved, but rejected because 5 markers were not detected. Ensure all 5 markers are visible to accept views.
                </small>
              ) : null}
            </Card>
          ) : null}
        </aside>
      </div>
      {selectedCalibration ? <BlobDetectorTuningModal open={tuningOpen} projectId={projectId} calibration={selectedCalibration} onClose={() => setTuningOpen(false)} onSaved={(saved) => { setCalibrations((items) => [saved, ...items.map((item) => ({ ...item, active: false }))]); setSelectedCalibrationId(saved.id); }} /> : null}
      <Modal open={importOpen} title="Import probe calibration" description="Validation is staged and has not changed the project." onRequestClose={() => setImportOpen(false)} size="sm" footer={<><Button onClick={() => setImportOpen(false)}>Cancel</Button><Button variant="primary" busy={busy} disabled={!validation?.valid} onClick={() => void importCalibration()}>Import & activate</Button></>}>
        {validation?.valid ? <><InlineAlert tone="success" title="Calibration passed validation">{validation.summary?.marker_point_count ?? 5} unique marker points · {validation.summary?.units ?? "m"} · RMS {validation.summary?.calibration_rms_px?.toFixed(2) ?? "—"} px</InlineAlert>{validation.warnings.map((warning) => <InlineAlert key={warning} tone="warning" title="Validation warning">{warning}</InlineAlert>)}</> : <InlineAlert tone="danger" title="Calibration was rejected">{validation?.errors?.map((item) => `${item.path}: ${item.message}`).join(" · ")}</InlineAlert>}
      </Modal>
    </div>
  );
}

function RegistrationStep({ index, title, detail, state, action }: { index: number; title: string; detail: string; state: "complete" | "active" | "pending"; action?: React.ReactNode }) {
  return <li className={`registration-step is-${state}`}><span>{state === "complete" ? "✓" : index}</span><div><strong>{title}</strong><p>{detail}</p></div>{action ? <div>{action}</div> : null}</li>;
}
