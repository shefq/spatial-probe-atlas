import { useEffect, useMemo, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { ReconnectingSocket, type BinaryStreamMessage } from "../../api/streams";
import type { ProbeCalibration, ProbeTestMetrics } from "../../api/types";
import { Button, Field, InlineAlert, Metric, Modal, StatusBadge } from "../../components/ui";
import { useUiStore } from "../../stores";

function parseTipOffsetMm(t_marker_tip?: number[]): [number, number, number] {
  if (!t_marker_tip || !Array.isArray(t_marker_tip)) return [0, 0, 150];
  if (t_marker_tip.length === 3) {
    return [
      Math.round(t_marker_tip[0] * 1000 * 10) / 10,
      Math.round(t_marker_tip[1] * 1000 * 10) / 10,
      Math.round(t_marker_tip[2] * 1000 * 10) / 10,
    ];
  }
  if (t_marker_tip.length === 16) {
    const tx = t_marker_tip[3] !== 0 || t_marker_tip[7] !== 0 || t_marker_tip[11] !== 0 ? t_marker_tip[3] : t_marker_tip[12];
    const ty = t_marker_tip[3] !== 0 || t_marker_tip[7] !== 0 || t_marker_tip[11] !== 0 ? t_marker_tip[7] : t_marker_tip[13];
    const tz = t_marker_tip[3] !== 0 || t_marker_tip[7] !== 0 || t_marker_tip[11] !== 0 ? t_marker_tip[11] : t_marker_tip[14];
    return [
      Math.round((tx || 0) * 1000 * 10) / 10,
      Math.round((ty || 0) * 1000 * 10) / 10,
      Math.round((tz || 0) * 1000 * 10) / 10,
    ];
  }
  return [0, 0, 150];
}

interface ProbeTipAdjustmentModalProps {
  open: boolean;
  projectId: string;
  calibration: ProbeCalibration;
  onSaved: (calibration: ProbeCalibration) => void;
  onClose: () => void;
}

export function ProbeTipAdjustmentModal({ open, projectId, calibration, onSaved, onClose }: ProbeTipAdjustmentModalProps) {
  const initialTip = useMemo(() => parseTipOffsetMm(calibration.probe?.t_marker_tip), [calibration]);
  const [tipMm, setTipMm] = useState<[number, number, number]>(initialTip);
  const [metrics, setMetrics] = useState<ProbeTestMetrics & { tip_2d?: [number, number] }>({ blob_count: 0, candidate_count: 0, inliers: 0, tracked: false });
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [streamState, setStreamState] = useState("closed");
  const [saving, setSaving] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const streamRef = useRef<ReconnectingSocket | null>(null);
  const objectUrls = useRef<string[]>([]);
  const pushToast = useUiStore((state) => state.pushToast);

  const dirty = useMemo(() => {
    return tipMm[0] !== initialTip[0] || tipMm[1] !== initialTip[1] || tipMm[2] !== initialTip[2];
  }, [tipMm, initialTip]);

  useEffect(() => {
    if (open) {
      setTipMm(parseTipOffsetMm(calibration.probe?.t_marker_tip));
      setStreamError(null);
    }
  }, [open, calibration]);

  useEffect(() => {
    if (!open) return;
    const stream = new ReconnectingSocket(`/ws/v1/projects/${projectId}/probe-tuning`, {
      onState: setStreamState,
      onError: setStreamError,
      onEnvelope: (envelope) => {
        if (envelope.type === "probe.tuning_result") {
          setMetrics(envelope.data as any);
        }
      },
      onBinary: (message: BinaryStreamMessage) => {
        const encoding = String(message.header?.encoding ?? "jpeg");
        const mime = encoding.includes("png") ? "image/png" : "image/jpeg";
        const url = URL.createObjectURL(new Blob([message.payload], { type: mime }));
        objectUrls.current.push(url);
        setImageUrl((prev) => {
          if (prev) URL.revokeObjectURL(prev);
          return url;
        });
      },
    });
    streamRef.current = stream;
    stream.connect();
    stream.send("subscribe", { calibration_id: calibration.id });

    return () => {
      stream.close();
      streamRef.current = null;
      objectUrls.current.forEach(URL.revokeObjectURL);
      objectUrls.current = [];
      setImageUrl(null);
    };
  }, [open, projectId, calibration.id]);

  // Patch live tip offset to backend in real time as sliders move
  useEffect(() => {
    if (!open || !streamRef.current) return;
    const timer = window.setTimeout(() => {
      streamRef.current?.send("tuning.patch", {
        tip_offset: [tipMm[0] / 1000.0, tipMm[1] / 1000.0, tipMm[2] / 1000.0],
      });
    }, 40);
    return () => window.clearTimeout(timer);
  }, [tipMm, open]);

  const updateAxis = (axisIdx: 0 | 1 | 2, val: number) => {
    setTipMm((prev) => {
      const next: [number, number, number] = [...prev];
      next[axisIdx] = Math.round(val * 10) / 10;
      return next;
    });
  };

  const nudge = (axisIdx: 0 | 1 | 2, delta: number) => {
    setTipMm((prev) => {
      const next: [number, number, number] = [...prev];
      next[axisIdx] = Math.round((next[axisIdx] + delta) * 10) / 10;
      return next;
    });
  };

  const save = async () => {
    setSaving(true);
    try {
      const tipOffsetMeters = [tipMm[0] / 1000.0, tipMm[1] / 1000.0, tipMm[2] / 1000.0];
      const updated = await api.probe.updateTip(projectId, calibration.id, tipOffsetMeters);
      onSaved(updated);
      pushToast({
        kind: "success",
        title: "Probe tip saved",
        message: `Tip offset updated to [${tipMm.map((n) => n.toFixed(1)).join(", ")}] mm.`,
      });
      onClose();
    } catch (e) {
      pushToast({
        kind: "error",
        title: "Failed to save tip position",
        message: errorMessage(e),
      });
    } finally {
      setSaving(false);
    }
  };

  const tipLengthMm = Math.sqrt(tipMm[0] ** 2 + tipMm[1] ** 2 + tipMm[2] ** 2);

  return (
    <Modal
      open={open}
      title="Adjust Probe Tip Position"
      description="Live camera alignment widget. Nudge XYZ offsets until the crosshair matches the physical probe tip."
      onRequestClose={onClose}
      size="lg"
      footer={
        <div className="button-row" style={{ display: "flex", justifyContent: "space-between", width: "100%" }}>
          <Button onClick={() => setTipMm(initialTip)} disabled={!dirty}>
            Reset
          </Button>
          <div className="button-row" style={{ display: "flex", gap: "8px" }}>
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" busy={saving} onClick={() => void save()}>
              Save & Apply Tip
            </Button>
          </div>
        </div>
      }
    >
      <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr", gap: "1.5rem", alignItems: "start" }}>
        {/* Camera Live Preview with Tip Overlay */}
        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <div
            style={{
              position: "relative",
              width: "100%",
              aspectRatio: "4 / 3",
              background: "#090d14",
              borderRadius: "8px",
              overflow: "hidden",
              border: "1px solid rgba(255,255,255,0.1)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            {imageUrl ? (
              <img
                src={imageUrl}
                alt="Live Probe Tip Preview"
                style={{ width: "100%", height: "100%", objectFit: "contain" }}
              />
            ) : (
              <div style={{ color: "#94a3b8", display: "flex", flexDirection: "column", alignItems: "center", gap: "8px" }}>
                <span className="spinner" />
                <span>{streamState === "open" ? "Streaming camera overlay…" : "Connecting camera stream…"}</span>
              </div>
            )}
            <div style={{ position: "absolute", top: "8px", right: "8px" }}>
              <StatusBadge state={metrics.tracked ? "passed" : "warning"} label={metrics.tracked ? "5/5 Tracked" : "Probe Lost"} />
            </div>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "0.5rem" }}>
            <Metric label="Total Length" value={`${tipLengthMm.toFixed(1)} mm`} />
            <Metric label="2D Projection" value={metrics.tip_2d ? `(${metrics.tip_2d[0].toFixed(0)}, ${metrics.tip_2d[1].toFixed(0)}) px` : "—"} />
            <Metric label="Reprojection" value={metrics.reprojection_error_px != null ? `${metrics.reprojection_error_px.toFixed(2)} px` : "—"} />
          </div>

          {streamError ? <InlineAlert tone="danger" title="Stream Error">{streamError}</InlineAlert> : null}
          {!metrics.tracked ? (
            <InlineAlert tone="warning" title="Probe not tracked">
              Ensure all 5 optical markers on the probe are clearly visible in the camera view to see the projected tip crosshair.
            </InlineAlert>
          ) : null}
        </div>

        {/* XYZ Adjustments */}
        <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
          <AxisNudgeControl
            label="X Axis Offset"
            value={tipMm[0]}
            color="#f43f5e"
            onChange={(v) => updateAxis(0, v)}
            onNudge={(d) => nudge(0, d)}
          />
          <AxisNudgeControl
            label="Y Axis Offset"
            value={tipMm[1]}
            color="#10b981"
            onChange={(v) => updateAxis(1, v)}
            onNudge={(d) => nudge(1, d)}
          />
          <AxisNudgeControl
            label="Z Axis (Length / Extension)"
            value={tipMm[2]}
            color="#06b6d4"
            onChange={(v) => updateAxis(2, v)}
            onNudge={(d) => nudge(2, d)}
          />

          <div style={{ borderTop: "1px solid rgba(255,255,255,0.08)", paddingTop: "0.75rem", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <small style={{ color: "#94a3b8" }}>
              Quick Z Flips:
            </small>
            <div style={{ display: "flex", gap: "6px" }}>
              <Button size="sm" onClick={() => updateAxis(2, -tipMm[2])}>
                Invert Z (±)
              </Button>
              <Button size="sm" onClick={() => setTipMm([0, 0, 150])}>
                Standard 150mm
              </Button>
            </div>
          </div>
        </div>
      </div>
    </Modal>
  );
}

function AxisNudgeControl({
  label,
  value,
  color,
  onChange,
  onNudge,
}: {
  label: string;
  value: number;
  color: string;
  onChange: (val: number) => void;
  onNudge: (delta: number) => void;
}) {
  return (
    <div style={{ background: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.07)", borderRadius: "8px", padding: "0.75rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.4rem" }}>
        <strong style={{ color, fontSize: "0.88rem", display: "flex", alignItems: "center", gap: "6px" }}>
          <span style={{ display: "inline-block", width: "8px", height: "8px", borderRadius: "50%", background: color }} />
          {label}
        </strong>
        <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
          <input
            type="number"
            step="0.1"
            value={Number.isFinite(value) ? value : 0}
            onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
            style={{
              width: "75px",
              background: "#090d14",
              border: "1px solid rgba(255,255,255,0.2)",
              borderRadius: "4px",
              color: "#f8fafc",
              padding: "3px 6px",
              textAlign: "right",
              fontSize: "0.85rem",
              fontWeight: 600,
            }}
          />
          <span style={{ fontSize: "0.8rem", color: "#94a3b8" }}>mm</span>
        </div>
      </div>

      <input
        type="range"
        min={-300}
        max={300}
        step={0.5}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        style={{ width: "100%", margin: "0.3rem 0 0.5rem 0", accentColor: color }}
      />

      <div style={{ display: "flex", gap: "4px", justifyContent: "space-between" }}>
        <button className="button button--default button--sm" style={{ padding: "2px 6px", fontSize: "0.75rem" }} onClick={() => onNudge(-10)}>
          -10
        </button>
        <button className="button button--default button--sm" style={{ padding: "2px 6px", fontSize: "0.75rem" }} onClick={() => onNudge(-1)}>
          -1
        </button>
        <button className="button button--default button--sm" style={{ padding: "2px 6px", fontSize: "0.75rem" }} onClick={() => onNudge(-0.1)}>
          -0.1
        </button>
        <button className="button button--default button--sm" style={{ padding: "2px 6px", fontSize: "0.75rem" }} onClick={() => onNudge(0.1)}>
          +0.1
        </button>
        <button className="button button--default button--sm" style={{ padding: "2px 6px", fontSize: "0.75rem" }} onClick={() => onNudge(1)}>
          +1
        </button>
        <button className="button button--default button--sm" style={{ padding: "2px 6px", fontSize: "0.75rem" }} onClick={() => onNudge(10)}>
          +10
        </button>
      </div>
    </div>
  );
}
