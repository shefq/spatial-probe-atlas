import { useEffect, useMemo, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { ReconnectingSocket, type BinaryStreamMessage } from "../../api/streams";
import { DEFAULT_BLOB_SETTINGS, type BlobDetectorSettings, type ProbeCalibration, type ProbeTestMetrics } from "../../api/types";
import { Button, Field, InlineAlert, Metric, Modal, StatusBadge, Toggle } from "../../components/ui";
import { useUiStore } from "../../stores";

const numericFields: Array<{
  key: keyof BlobDetectorSettings;
  label: string;
  min: number;
  max?: number;
  step: number;
  integer?: boolean;
  group?: keyof BlobDetectorSettings;
}> = [
  { key: "minThreshold", label: "Minimum threshold", min: 0, max: 255, step: 1 },
  { key: "maxThreshold", label: "Maximum threshold", min: 0, max: 255, step: 1 },
  { key: "thresholdStep", label: "Threshold step", min: 0.1, max: 255, step: 0.5 },
  { key: "minRepeatability", label: "Minimum repeatability", min: 1, max: 100, step: 1, integer: true },
  { key: "minDistBetweenBlobs", label: "Minimum blob distance (px)", min: 0, max: 1000, step: 0.5 },
  { key: "blobColor", label: "Blob colour (0 black, 255 white)", min: 0, max: 255, step: 1, integer: true, group: "filterByColor" },
  { key: "minArea", label: "Minimum area (px²)", min: 0, max: 5000, step: 1, group: "filterByArea" },
  { key: "maxArea", label: "Maximum area (px²)", min: 0.01, max: 10000, step: 1, group: "filterByArea" },
  { key: "minCircularity", label: "Minimum circularity", min: 0, max: 1, step: 0.01, group: "filterByCircularity" },
  { key: "maxCircularity", label: "Maximum circularity", min: 0, max: 1, step: 0.01, group: "filterByCircularity" },
  { key: "minInertiaRatio", label: "Minimum inertia ratio", min: 0, max: 1, step: 0.01, group: "filterByInertia" },
  { key: "maxInertiaRatio", label: "Maximum inertia ratio", min: 0, max: 1, step: 0.01, group: "filterByInertia" },
  { key: "minConvexity", label: "Minimum convexity", min: 0, max: 1, step: 0.01, group: "filterByConvexity" },
  { key: "maxConvexity", label: "Maximum convexity", min: 0, max: 1, step: 0.01, group: "filterByConvexity" },
];

const booleanFields: Array<{ key: keyof BlobDetectorSettings; label: string; description: string }> = [
  { key: "filterByColor", label: "Filter by colour", description: "Select black or white candidate centres." },
  { key: "filterByArea", label: "Filter by area", description: "Reject blobs outside a pixel-area interval." },
  { key: "filterByCircularity", label: "Filter by circularity", description: "Prefer round marker projections." },
  { key: "filterByInertia", label: "Filter by inertia", description: "Reject highly elongated blobs." },
  { key: "filterByConvexity", label: "Filter by convexity", description: "Reject concave connected components." },
];

export function validateBlobSettings(value: BlobDetectorSettings): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!(value.minThreshold < value.maxThreshold)) errors.maxThreshold = "Maximum must be greater than minimum.";
  if (!(value.thresholdStep > 0)) errors.thresholdStep = "Threshold step must be positive.";
  if (!Number.isInteger(value.minRepeatability) || value.minRepeatability < 1) errors.minRepeatability = "Use an integer of at least 1.";
  const ranges: Array<[boolean, keyof BlobDetectorSettings, keyof BlobDetectorSettings]> = [
    [value.filterByArea, "minArea", "maxArea"],
    [value.filterByCircularity, "minCircularity", "maxCircularity"],
    [value.filterByInertia, "minInertiaRatio", "maxInertiaRatio"],
    [value.filterByConvexity, "minConvexity", "maxConvexity"],
  ];
  ranges.forEach(([enabled, minKey, maxKey]) => {
    if (enabled && Number(value[minKey]) > Number(value[maxKey])) errors[maxKey] = "Maximum must be greater than or equal to minimum.";
  });
  numericFields.forEach((field) => {
    const number = Number(value[field.key]);
    if (!Number.isFinite(number)) errors[field.key] = "Enter a finite number.";
    else if (number < field.min || (field.max !== undefined && number > field.max)) errors[field.key] = `Use ${field.min} to ${field.max ?? "a larger value"}.`;
  });
  return errors;
}

export function BlobDetectorTuningModal({ open, projectId, calibration, onSaved, onClose }: {
  open: boolean;
  projectId: string;
  calibration: ProbeCalibration;
  onSaved: (calibration: ProbeCalibration) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<BlobDetectorSettings>({ ...calibration.blob_detector });
  const [metrics, setMetrics] = useState<ProbeTestMetrics>({ blob_count: 0, candidate_count: 0, inliers: 0, tracked: false });
  const [images, setImages] = useState<Record<string, string>>({});
  const [streamState, setStreamState] = useState("closed");
  const [confirmClose, setConfirmClose] = useState(false);
  const [saving, setSaving] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const streamRef = useRef<ReconnectingSocket | null>(null);
  const objectUrls = useRef<string[]>([]);
  const setDraftDirty = useUiStore((state) => state.setDraftDirty);
  const pushToast = useUiStore((state) => state.pushToast);
  const errors = useMemo(() => validateBlobSettings(draft), [draft]);
  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(calibration.blob_detector), [calibration.blob_detector, draft]);

  useEffect(() => {
    if (!open) return;
    setDraft({ ...calibration.blob_detector }); setConfirmClose(false); setImportError(null); setStreamError(null);
  }, [open, calibration.id, calibration.revision]);
  useEffect(() => { setDraftDirty("probe-tuning", open && dirty); return () => setDraftDirty("probe-tuning", false); }, [dirty, open, setDraftDirty]);
  useEffect(() => {
    if (!open) return;
    const stream = new ReconnectingSocket(`/ws/v1/projects/${projectId}/probe-tuning`, {
      onState: setStreamState,
      onError: setStreamError,
      onEnvelope: (envelope) => {
        if (envelope.type === "probe.tuning_result") setMetrics(envelope.data as ProbeTestMetrics);
      },
      onBinary: (message) => setDiagnosticImage(message, setImages, objectUrls),
    });
    streamRef.current = stream;
    stream.connect();
    stream.send("subscribe", { calibration_id: calibration.id });
    return () => {
      stream.close(); streamRef.current = null;
      objectUrls.current.forEach(URL.revokeObjectURL); objectUrls.current = [];
      setImages({});
    };
  }, [open, projectId, calibration.id]);
  useEffect(() => {
    if (!open || Object.keys(errors).length) return;
    const timer = window.setTimeout(() => streamRef.current?.send("tuning.patch", { calibration_id: calibration.id, blob_detector: draft }), 75);
    return () => window.clearTimeout(timer);
  }, [draft, errors, open, calibration.id]);

  const patchValue = (key: keyof BlobDetectorSettings, value: number | boolean) => setDraft((current) => ({ ...current, [key]: value }));
  const requestClose = () => { if (dirty) setConfirmClose(true); else onClose(); };
  const discard = () => { setDraft({ ...calibration.blob_detector }); setConfirmClose(false); onClose(); };
  const save = async (closeAfter = true) => {
    if (Object.keys(errors).length) return;
    setSaving(true);
    try {
      const saved = await api.probe.createRevision(projectId, calibration.id, draft);
      onSaved(saved); pushToast({ kind: "success", title: "Probe settings saved", message: `Revision ${saved.revision} is active.` });
      setConfirmClose(false); if (closeAfter) onClose();
    } catch (value) { pushToast({ kind: "error", title: "Settings were not saved", message: errorMessage(value) }); }
    finally { setSaving(false); }
  };
  const importDraft = async (file: File) => {
    setImportError(null);
    try {
      const parsed = JSON.parse(await file.text()) as { blob_detector?: BlobDetectorSettings };
      if (!parsed.blob_detector) throw new Error("The file does not contain blob_detector settings.");
      const keys = [...numericFields.map((field) => field.key), ...booleanFields.map((field) => field.key)];
      if (keys.some((key) => !(key in parsed.blob_detector!))) throw new Error("The detector settings are incomplete; all 19 fields are required.");
      setDraft({ ...parsed.blob_detector });
    } catch (value) { setImportError(value instanceof Error ? value.message : "The selected file is invalid."); }
  };
  const downloadDraft = () => {
    const portable = {
      schema_version: calibration.schema_version,
      calibration_id: calibration.calibration_id ?? calibration.id,
      name: calibration.name,
      created_at: calibration.created_at ?? new Date().toISOString(),
      units: calibration.units,
      probe: calibration.probe,
      blob_detector: draft,
      quality: calibration.quality,
      provenance: calibration.provenance ?? { application_version: "1.0.0", method: "imported" },
    };
    const url = URL.createObjectURL(new Blob([JSON.stringify(portable, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "probe_calibration.json"; link.click(); URL.revokeObjectURL(url);
  };

  return (
    <>
      <Modal open={open} title="Advanced blob detector" description="Tune a live draft against Record3D. Nothing replaces the active project calibration until you save." onRequestClose={requestClose} size="xl" testId="blob-tuning-modal" footer={<><Button onClick={requestClose}>Cancel</Button><label className="button button--default button--md file-button">Import settings<input data-testid="blob-import" type="file" accept=".json,application/json" onChange={(event) => { const file = event.target.files?.[0]; if (file) void importDraft(file); event.target.value = ""; }} /></label><Button onClick={downloadDraft}>Download settings</Button><Button variant="primary" busy={saving} disabled={Boolean(Object.keys(errors).length)} onClick={() => void save()}>Save to current project</Button></>}>
        <div className="tuning-layout">
          <section className="tuning-diagnostics">
            <div className="tuning-stream-status"><StatusBadge state={streamState} label={streamState === "open" ? "Live tuning" : streamState} /><StatusBadge state={metrics.tracked ? "tracked" : "lost"} label={metrics.tracked ? "5/5 tracked" : `${metrics.inliers}/5 tracked`} /></div>
            <div className="diagnostic-views"><DiagnosticView title="Raw Record3D" url={images.raw ?? metrics.raw_image_url} /><DiagnosticView title="Threshold / binary" url={images.binary ?? metrics.binary_image_url} /><DiagnosticView title="Detected overlay" url={images.overlay ?? metrics.overlay_image_url} /></div>
            <div className="metric-grid"><Metric label="Blobs" value={metrics.blob_count} /><Metric label="Candidates" value={metrics.candidate_count} /><Metric label="Inliers" value={`${metrics.inliers} / 5`} tone={metrics.inliers === 5 ? "good" : "warning"} /><Metric label="Reprojection" value={metrics.reprojection_error_px == null ? "—" : `${metrics.reprojection_error_px.toFixed(2)} px`} /></div>
            {streamError ? <InlineAlert tone="warning" title="Tuning stream interrupted">{streamError} The client will reconnect without saving the draft.</InlineAlert> : null}
            {metrics.rejection_reason || metrics.exposure_feedback ? <InlineAlert tone="warning" title={metrics.rejection_reason ?? "Exposure guidance"}>{metrics.exposure_feedback ?? "Move the probe into view and adjust enabled filters."}</InlineAlert> : null}
          </section>
          <section className="tuning-controls">
            <div className="tuning-controls__header"><div><h3>SimpleBlobDetector parameters</h3><p>All supported v1 values are exposed. Disabled filter groups remain visible.</p></div><Button size="sm" onClick={() => setDraft({ ...DEFAULT_BLOB_SETTINGS })}>Reset to defaults</Button></div>
            {importError ? <InlineAlert tone="danger" title="Settings not imported">{importError} The current draft was preserved.</InlineAlert> : null}
            <div className="detector-form">
              <fieldset><legend>Threshold sweep & grouping</legend><div className="field-grid">{numericFields.filter((field) => !field.group).map((field) => <NumericSetting key={field.key} field={field} value={Number(draft[field.key])} error={errors[field.key]} onChange={(value) => patchValue(field.key, value)} />)}</div></fieldset>
              {booleanFields.map((group) => {
                const enabled = Boolean(draft[group.key]);
                const children = numericFields.filter((field) => field.group === group.key);
                return <fieldset key={group.key} className={!enabled ? "is-disabled" : ""}><legend><Toggle label={group.label} checked={enabled} onChange={(event) => patchValue(group.key, event.target.checked)} /></legend><p>{group.description}</p><div className="field-grid">{children.map((field) => <NumericSetting key={field.key} field={field} value={Number(draft[field.key])} error={errors[field.key]} disabled={!enabled} onChange={(value) => patchValue(field.key, value)} />)}</div></fieldset>;
              })}
            </div>
          </section>
        </div>
      </Modal>
      <Modal open={confirmClose} title="Unsaved detector settings" description="Closing now can discard the live draft or save it as a new active calibration revision." onRequestClose={() => setConfirmClose(false)} size="sm" footer={<><Button onClick={() => setConfirmClose(false)}>Keep editing</Button><Button variant="danger" onClick={discard} data-testid="discard-blob-draft">Discard</Button><Button variant="primary" busy={saving} disabled={Boolean(Object.keys(errors).length)} onClick={() => void save()}>Save</Button></>}>
        <InlineAlert tone="warning" title="The active calibration is still unchanged">Discard restores the saved detector configuration. Geometry and prior revisions are never deleted.</InlineAlert>
      </Modal>
    </>
  );
}

function NumericSetting({ field, value, error, disabled, onChange }: { field: typeof numericFields[number]; value: number; error?: string; disabled?: boolean; onChange: (value: number) => void }) {
  const minVal = field.min ?? 0;
  const maxVal = field.max ?? (field.key === "minArea" ? 5000 : field.key === "maxArea" ? 10000 : 100);
  const numVal = Number.isFinite(value) ? value : minVal;

  return (
    <Field label={field.label} error={error}>
      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
        <input
          type="range"
          min={minVal}
          max={maxVal}
          step={field.step}
          value={numVal}
          disabled={disabled}
          onChange={(event) => {
            const number = Number(event.target.value);
            onChange(field.integer ? Math.round(number) : number);
          }}
          style={{ flex: 1, accentColor: "#58d6ff", cursor: disabled ? "not-allowed" : "pointer" }}
        />
        <input
          className="input"
          type="number"
          data-testid={`blob-${field.key}`}
          value={Number.isFinite(value) ? value : ""}
          min={field.min}
          max={field.max}
          step={field.step}
          disabled={disabled}
          onChange={(event) => {
            const number = Number(event.target.value);
            onChange(field.integer ? Math.round(number) : number);
          }}
          style={{ width: "75px", textAlign: "right" }}
        />
      </div>
    </Field>
  );
}

function DiagnosticView({ title, url }: { title: string; url?: string }) {
  return <figure><div>{url ? <img src={url} alt={title} /> : <span><span className="spinner" /> Waiting for frame</span>}</div><figcaption>{title}</figcaption></figure>;
}

function setDiagnosticImage(message: BinaryStreamMessage, setImages: React.Dispatch<React.SetStateAction<Record<string, string>>>, objectUrls: React.MutableRefObject<string[]>) {
  const channel = String(message.header.kind ?? message.header.channel ?? "overlay").replace("probe.", "");
  const encoding = String(message.header.encoding ?? "jpeg");
  const url = URL.createObjectURL(new Blob([message.payload], { type: encoding.includes("png") ? "image/png" : "image/jpeg" }));
  objectUrls.current.push(url);
  setImages((current) => {
    const previous = current[channel]; if (previous) URL.revokeObjectURL(previous);
    return { ...current, [channel]: url };
  });
}
