import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import { LatestFrameBuffer, ReconnectingSocket, type BinaryStreamMessage } from "../api/streams";
import type { CalibrationValidation, CameraDevice, CameraStatus } from "../api/types";
import { Button, Card, EmptyState, Field, InlineAlert, Metric, Segmented, Skeleton, StatusBadge } from "../components/ui";
import { useCameraStore, useUiStore } from "../stores";
import { formatCount, formatDuration } from "../utils/format";

const replayDevice: CameraDevice = { device_id: "replay:synthetic", adapter: "replay", name: "Synthetic Record3D replay", available: true, detail: "Deterministic fixture — no hardware required" };

export function CameraSetupPage() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const devices = useCameraStore((state) => state.devices);
  const setDevices = useCameraStore((state) => state.setDevices);
  const status = useCameraStore((state) => state.status);
  const setStatus = useCameraStore((state) => state.setStatus);
  const previewMode = useCameraStore((state) => state.previewMode);
  const setPreviewMode = useCameraStore((state) => state.setPreviewMode);
  const previewQuality = useCameraStore((state) => state.previewQuality);
  const setPreviewQuality = useCameraStore((state) => state.setPreviewQuality);
  const pushToast = useUiStore((state) => state.pushToast);
  const [selectedId, setSelectedId] = useState("");
  const [enumerating, setEnumerating] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [calibration, setCalibration] = useState<CalibrationValidation | null>(null);
  const [calibrationFile, setCalibrationFile] = useState<File | null>(null);

  const enumerate = async (signal?: AbortSignal) => {
    setEnumerating(true);
    setError(null);
    try {
      const discovered = await api.camera.devices(signal);
      const withReplay = discovered.some((device) => device.adapter === "replay") ? discovered : [...discovered, replayDevice];
      setDevices(withReplay);
      setSelectedId((current) => current || withReplay.find((device) => device.available && !device.busy)?.device_id || "");
      setStatus(await api.camera.status(signal));
    } catch (value) {
      if (!signal?.aborted) {
        setDevices([replayDevice]);
        setSelectedId("replay:synthetic");
        setError(errorMessage(value));
      }
    } finally { if (!signal?.aborted) setEnumerating(false); }
  };

  useEffect(() => {
    const controller = new AbortController();
    void enumerate(controller.signal);
    return () => controller.abort();
  }, []);

  const selected = devices.find((device) => device.device_id === selectedId);
  const connect = async () => {
    if (!selected) return;
    setBusy(true); setError(null);
    setStatus({ state: "opening", device: selected });
    try { setStatus(await api.camera.connect(projectId, selected)); }
    catch (value) { setError(errorMessage(value)); setStatus({ state: "error", device: selected, error: errorMessage(value) }); }
    finally { setBusy(false); }
  };
  const disconnect = async () => {
    setBusy(true);
    try { await api.camera.disconnect(); setStatus({ state: "disconnected" }); }
    catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const validateCalibration = async (file: File) => {
    setCalibrationFile(file); setCalibration(null); setBusy(true);
    try { setCalibration(await api.camera.validateCalibration(projectId, file)); }
    catch (value) { pushToast({ kind: "error", title: "Calibration is not compatible", message: errorMessage(value) }); }
    finally { setBusy(false); }
  };
  const importCalibration = async () => {
    if (!calibration?.valid) return;
    setBusy(true);
    try {
      const imported = await api.camera.importCalibration(projectId, calibration.validation_id);
      await api.camera.activateCalibration(projectId, imported.id);
      pushToast({ kind: "success", title: "External calibration activated", message: calibrationFile?.name });
      setCalibration(null); setCalibrationFile(null);
    } catch (value) { pushToast({ kind: "error", title: "Calibration import failed", message: errorMessage(value) }); }
    finally { setBusy(false); }
  };
  const ready = status.state === "ready";

  return (
    <div className="page page--workflow">
      <header className="page-heading"><div><div className="eyebrow">STEP 1 · ACQUISITION</div><h1>Camera Setup</h1><p>Connect Record3D and verify synchronized colour, depth and per-frame intrinsics.</p></div><div className="page-heading__actions">{ready ? <Button variant="primary" onClick={() => navigate(`/projects/${projectId}/mapping`)}>Continue to mapping →</Button> : null}</div></header>
      {error ? <InlineAlert tone={devices.length === 1 ? "warning" : "danger"} title="Camera service needs attention" action={<Button size="sm" onClick={() => void enumerate()}>Retry enumeration</Button>}>{error} The deterministic replay adapter remains available for local validation.</InlineAlert> : null}
      <div className="workflow-grid workflow-grid--camera">
        <Card className="preview-card" title="Live synchronized preview" eyebrow={selected?.adapter === "replay" ? "REPLAY SOURCE" : "RECORD3D USB"} actions={<><Segmented value={previewMode} label="Preview channel" options={[{ value: "rgb", label: "RGB" }, { value: "depth", label: "Depth" }, { value: "split", label: "Split" }]} onChange={setPreviewMode} /><select className="select select--compact" aria-label="Preview quality" value={previewQuality} onChange={(event) => setPreviewQuality(event.target.value as typeof previewQuality)}><option value="low">Low bandwidth</option><option value="medium">Balanced</option><option value="high">High detail</option></select></>}>
          <CameraPreview active={status.state !== "disconnected" && status.state !== "error"} mode={previewMode} quality={previewQuality} onHealth={(health) => setStatus({ ...status, ...health })} />
        </Card>
        <aside className="workflow-sidebar">
          <Card title="Device" eyebrow="SOURCE">
            {enumerating ? <Skeleton lines={4} /> : devices.length ? (
              <div className="device-list" role="radiogroup" aria-label="Available cameras">{devices.map((device) => <label key={device.device_id} className={`device-option ${selectedId === device.device_id ? "is-selected" : ""} ${!device.available || device.busy ? "is-disabled" : ""}`}><input type="radio" name="camera" value={device.device_id} checked={selectedId === device.device_id} disabled={!device.available || device.busy || status.state !== "disconnected"} onChange={() => setSelectedId(device.device_id)} /><span className="device-option__icon">{device.adapter === "replay" ? "▶" : "▣"}</span><span><strong>{device.name}</strong><small>{device.detail ?? device.device_id}</small></span><StatusBadge state={device.busy ? "warning" : device.available ? "ready" : "inactive"} label={device.busy ? "busy" : device.available ? "available" : "offline"} /></label>)}</div>
            ) : <EmptyState icon="⌁" title="No camera found">Connect and unlock the iPhone, open Record3D, trust this computer, then enumerate again.</EmptyState>}
            <div className="button-row">{status.state === "disconnected" || status.state === "error" ? <Button variant="primary" busy={busy} disabled={!selected} onClick={() => void connect()}>{selected?.adapter === "replay" ? "Start replay" : "Connect"}</Button> : <Button variant="danger" busy={busy} onClick={() => void disconnect()}>Disconnect</Button>}<Button disabled={busy || status.state !== "disconnected"} onClick={() => void enumerate()}>Refresh</Button></div>
          </Card>
          <Card title="Connection health" eyebrow="5-FRAME VERIFICATION" actions={<StatusBadge state={status.state} />}>
            <div className="health-checklist">
              <HealthItem label="Monotonic complete frames" value={(status.complete_frame_streak ?? 0) >= 5} detail={`${status.complete_frame_streak ?? 0} / 5`} />
              <HealthItem label="RGB stream" value={Boolean(status.rgb_width && status.rgb_height)} detail={status.rgb_width ? `${status.rgb_width} × ${status.rgb_height}` : undefined} />
              <HealthItem label="Depth stream aligned" value={status.depth_aligned === true} detail={status.depth_width ? `${status.depth_width} × ${status.depth_height}` : undefined} />
              <HealthItem label="Finite per-frame intrinsics" value={status.intrinsics_source === "record3d_per_frame" || Boolean(status.intrinsic_matrix)} />
              <HealthItem label="Sustained frame rate" value={(status.fps ?? 0) >= 10} detail={status.fps ? `${status.fps.toFixed(1)} FPS` : undefined} warning={(status.fps ?? 20) < 10} />
            </div>
            <div className="metric-grid"><Metric label="Latency" value={status.latency_ms == null ? "—" : `${status.latency_ms.toFixed(0)} ms`} /><Metric label="Frames" value={formatCount(status.frames_received)} /><Metric label="Dropped" value={formatCount(status.dropped_frames)} tone={(status.dropped_frames ?? 0) > 0 ? "warning" : undefined} /><Metric label="Connected" value={formatDuration(status.connected_seconds)} /></div>
          </Card>
          <Card title="Camera intrinsics" eyebrow="AUTHORITATIVE SOURCE">
            {selected?.adapter !== "external" ? <InlineAlert tone="success" title="Intrinsics supplied per frame">Record3D calibration is used automatically for the exact frame resolution. No camera-calibration wizard is required.</InlineAlert> : null}
            <details className="details-panel"><summary>Use an external camera calibration</summary><p className="muted">Import OpenCV JSON/YAML or ROS camera_info.yaml. Validation is non-mutating and checks model, coefficients and resolution.</p><Field label="Calibration file"><input className="file-input" type="file" accept=".json,.yaml,.yml,application/json" onChange={(event) => { const file = event.target.files?.[0]; if (file) void validateCalibration(file); }} /></Field>{calibration ? <InlineAlert tone={calibration.valid ? "success" : "danger"} title={calibration.valid ? "Calibration validated" : "Calibration rejected"}>{calibration.valid ? `${calibration.schema_version ?? "1.0.0"}; confirm to import and activate.` : calibration.errors?.map((item) => `${item.path}: ${item.message}`).join(" · ")} {calibration.valid ? <Button size="sm" busy={busy} onClick={() => void importCalibration()}>Import & activate</Button> : null}</InlineAlert> : null}</details>
          </Card>
        </aside>
      </div>
    </div>
  );
}

function HealthItem({ label, value, detail, warning = false }: { label: string; value: boolean; detail?: string; warning?: boolean }) {
  return <div className="health-item"><span className={value ? "health-item__check is-ready" : warning ? "health-item__check is-warning" : "health-item__check"}>{value ? "✓" : warning ? "△" : "·"}</span><span><strong>{label}</strong>{detail ? <small>{detail}</small> : null}</span></div>;
}

function CameraPreview({ active, mode, quality, onHealth }: { active: boolean; mode: "rgb" | "depth" | "split"; quality: string; onHealth: (health: Partial<CameraStatus>) => void }) {
  const [rgbUrl, setRgbUrl] = useState<string | null>(null);
  const [depthUrl, setDepthUrl] = useState<string | null>(null);
  const [state, setState] = useState("closed");
  const urls = useRef<string[]>([]);
  const onBinary = (message: BinaryStreamMessage) => {
    const header = message.header;
    const kind = String(header.kind ?? header.channel ?? "rgb");
    const encoding = String(header.encoding ?? (kind === "rgb" ? "jpeg" : "png"));
    const mime = encoding.includes("jpeg") || encoding.includes("jpg") ? "image/jpeg" : "image/png";
    const url = URL.createObjectURL(new Blob([message.payload], { type: mime }));
    urls.current.push(url);
    if (kind.includes("depth")) setDepthUrl((previous) => { if (previous) URL.revokeObjectURL(previous); return url; });
    else setRgbUrl((previous) => { if (previous) URL.revokeObjectURL(previous); return url; });
  };
  useEffect(() => {
    if (!active) { setState("closed"); return; }
    const latest = new LatestFrameBuffer<BinaryStreamMessage>();
    let renderFrame = 0;
    const stream = new ReconnectingSocket("/ws/v1/camera/preview", {
      onState: setState,
      onBinary: (message) => latest.push(message),
      onEnvelope: (envelope) => { if (envelope.type === "camera.health") onHealth(envelope.data as Partial<CameraStatus>); },
    });
    stream.connect();
    stream.send("subscribe", { channels: mode === "split" ? ["rgb", "depth"] : [mode], quality });
    const consume = () => {
      if (!document.hidden) { const message = latest.take(); if (message) onBinary(message); }
      renderFrame = requestAnimationFrame(consume);
    };
    renderFrame = requestAnimationFrame(consume);
    return () => { cancelAnimationFrame(renderFrame); stream.close(); urls.current.forEach(URL.revokeObjectURL); urls.current = []; setRgbUrl(null); setDepthUrl(null); };
  }, [active, mode, quality]);
  if (!active) return <div className="camera-placeholder"><span>▣</span><strong>Preview starts after connection</strong><small>RGB, depth and frame health remain local to this application.</small></div>;
  return <div className={`camera-preview camera-preview--${mode}`}><div className="preview-state"><StatusBadge state={state} label={state === "open" ? "Live" : state} /></div>{mode !== "depth" ? <PreviewPane url={rgbUrl} label="RGB" /> : null}{mode !== "rgb" ? <PreviewPane url={depthUrl} label="Depth" /> : null}</div>;
}

function PreviewPane({ url, label }: { url: string | null; label: string }) {
  return <div className="preview-pane">{url ? <img src={url} alt={`${label} camera preview`} /> : <div className="preview-wait"><span className="spinner" /> Waiting for {label.toLowerCase()} frame…</div>}<span className="preview-label">{label}</span></div>;
}
