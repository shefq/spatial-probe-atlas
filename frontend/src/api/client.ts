import type {
  ApiErrorPayload, AppSettings, CalibrationValidation, CameraDevice, CameraStatus, Capabilities, CaptureFrame,
  CaptureSet, DiagnosticCheck, ExportSnapshot, JobSnapshot, PagedResult, PaintedRecord, PointCloudManifest,
  ProbeCalibration, Project, ProjectSummary, Registration, ResourceSnapshot, ReviewFilters, SceneMap, MapTransform,
  SessionSnapshot, SessionSummary,
} from "./types";

const API_PREFIX = "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: Record<string, unknown>;
  readonly traceId?: string;
  readonly retryable: boolean;
  readonly suggestedAction?: string;
  constructor(status: number, payload?: ApiErrorPayload) {
    const error = payload?.error;
    super(error?.message ?? `Request failed (${status})`);
    this.name = "ApiError"; this.status = status; this.code = error?.code ?? "HTTP_ERROR"; this.details = error?.details;
    this.traceId = error?.trace_id; this.retryable = error?.retryable ?? status >= 500; this.suggestedAction = error?.suggested_action;
  }
}

const commandId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
function queryString(values: Record<string, string | number | boolean | undefined>): string {
  const query = new URLSearchParams(); Object.entries(values).forEach(([key, value]) => { if (value !== undefined && value !== "") query.set(key, String(value)); }); const text = query.toString(); return text ? `?${text}` : "";
}
export function normalizeReviewFilterTimestamp(value?: string): string | undefined {
  if (!value) return undefined;
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toISOString();
}
export function normalizeReviewFiltersForApi(filters: ReviewFilters): ReviewFilters {
  return {
    ...filters,
    from: normalizeReviewFilterTimestamp(filters.from),
    to: normalizeReviewFilterTimestamp(filters.to),
  };
}
function reviewQueryString(filters: ReviewFilters, cursor?: string): string {
  const normalized = normalizeReviewFiltersForApi(filters);
  return queryString({
    cursor,
    type: normalized.type,
    quality: normalized.quality,
    from: normalized.from,
    to: normalized.to,
    include_deleted: normalized.include_deleted,
  });
}
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers); headers.set("Accept", "application/json");
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (init.method && !["GET", "HEAD"].includes(init.method)) headers.set("Idempotency-Key", headers.get("Idempotency-Key") ?? commandId());
  let response: Response;
  try { response = await fetch(`${API_PREFIX}${path}`, { ...init, headers, credentials: "same-origin" }); }
  catch (value) { throw new ApiError(0, { error: { code: "NETWORK_UNAVAILABLE", message: value instanceof Error ? value.message : "The local service is unavailable.", retryable: true, suggested_action: "Start the Spatial Probe Atlas service and retry." } }); }
  if (!response.ok) {
    let payload: ApiErrorPayload | undefined; try { payload = await response.json() as ApiErrorPayload; } catch { /* typed fallback */ }
    throw new ApiError(response.status, payload);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
async function upload<T>(path: string, file: File): Promise<T> { const body = new FormData(); body.set("file", file); return request<T>(path, { method: "POST", body }); }
export function downloadFromApi(path: string, filename?: string): void { const link = document.createElement("a"); link.href = `${API_PREFIX}${path}`; if (filename) link.download = filename; link.rel = "noopener"; document.body.appendChild(link); link.click(); link.remove(); }

type FrameImportItem = { width: number; height: number; intrinsic_matrix: number[]; rgb_base64: string; depth_f32_base64: string; timestamp_ns?: number };
async function readFrameBundles(files: File[]): Promise<{ frames: FrameImportItem[] }> {
  if (!files.length) throw new Error("Choose at least one versioned JSON frame bundle.");
  const frames: FrameImportItem[] = [];
  for (const file of files) {
    const value = JSON.parse(await file.text()) as { frames?: FrameImportItem[] };
    if (!Array.isArray(value.frames) || !value.frames.length) throw new Error(`${file.name} does not contain a non-empty frames array.`);
    for (const frame of value.frames) {
      if (!(frame.width > 0 && frame.height > 0) || frame.intrinsic_matrix?.length !== 9 || !frame.rgb_base64 || !frame.depth_f32_base64) throw new Error(`${file.name} contains an incomplete frame. Width, height, 3x3 intrinsics, RGB and float32 depth are required.`);
      frames.push(frame);
    }
  }
  if (frames.length > 500) throw new Error("A single import command is limited to 500 frames.");
  return { frames };
}

export const api = {
  system: {
    capabilities: (signal?: AbortSignal) => request<Capabilities>("/system/capabilities", { signal }),
    resources: (signal?: AbortSignal) => request<ResourceSnapshot>("/system/resources", { signal }),
    diagnostics: () => request<{ checks: DiagnosticCheck[]; job_id?: string }>("/system/diagnostics", { method: "POST" }),
    supportBundle: () => request<{ job_id: string }>("/support-bundles", { method: "POST" }),
    settings: () => request<AppSettings>("/settings"),
    updateSettings: (patch: Partial<AppSettings>) => request<AppSettings>("/settings", { method: "PATCH", body: JSON.stringify(patch) }),
  },
  projects: {
    list: (includeArchived = false, signal?: AbortSignal) => request<PagedResult<Project> | Project[]>(`/projects${queryString({ include_archived: includeArchived })}`, { signal }),
    create: (name: string) => request<Project>("/projects", { method: "POST", body: JSON.stringify({ name }) }),
    get: (id: string, signal?: AbortSignal) => request<Project>(`/projects/${id}`, { signal }),
    summary: (id: string, signal?: AbortSignal) => request<ProjectSummary>(`/projects/${id}/summary`, { signal }),
    rename: (id: string, name: string, revision?: number) => request<Project>(`/projects/${id}`, { method: "PATCH", headers: revision === undefined ? undefined : { "If-Match": String(revision) }, body: JSON.stringify({ name }) }),
    clone: (id: string, name?: string) => request<{ project: Project; job_id?: string }>(`/projects/${id}/clone`, { method: "POST", body: JSON.stringify({ name }) }),
    archive: (id: string) => request<Project>(`/projects/${id}/archive`, { method: "POST" }),
    restore: (id: string) => request<Project>(`/projects/${id}/restore`, { method: "POST" }),
    reveal: (id: string) => request<void>(`/projects/${id}/reveal`, { method: "POST" }),
    importLegacy: (sourceDirectory: string, projectName: string | undefined, confirmDefaultedProbeSettings: boolean) => request<JobSnapshot & { target_project_id: string }>("/legacy-imports", { method: "POST", body: JSON.stringify({ source_directory: sourceDirectory, project_name: projectName || undefined, confirm_defaulted_probe_settings: confirmDefaultedProbeSettings }) }),
    legacyImport: (jobId: string) => request<JobSnapshot & { target_project_id: string }>(`/legacy-imports/${jobId}`),
    downloadLegacyReport: (jobId: string) => downloadFromApi(`/legacy-imports/${jobId}/report`, `spatial-probe-atlas-migration-${jobId}.json`),
  },
  camera: {
    devices: (signal?: AbortSignal) => request<CameraDevice[]>("/camera/devices", { signal }), status: (signal?: AbortSignal) => request<CameraStatus>("/camera/status", { signal }),
    connect: (projectId: string, device: CameraDevice) => request<CameraStatus>("/camera/connect", { method: "POST", body: JSON.stringify({ project_id: projectId, adapter: device.adapter, device_id: device.device_id }) }),
    disconnect: () => request<void>("/camera/disconnect", { method: "POST" }),
    validateCalibration: (projectId: string, file: File) => upload<CalibrationValidation>(`/projects/${projectId}/camera-calibrations/validate`, file),
    importCalibration: (projectId: string, validationId: string) => request<{ id: string }>(`/projects/${projectId}/camera-calibrations/import`, { method: "POST", body: JSON.stringify({ validation_id: validationId, activate: false }) }),
    activateCalibration: (projectId: string, id: string) => request<void>(`/projects/${projectId}/camera-calibrations/${id}/activate`, { method: "POST" }),
  },
  capture: {
    sets: (projectId: string, signal?: AbortSignal) => request<CaptureSet[]>(`/projects/${projectId}/capture-sets`, { signal }),
    createSet: (projectId: string, name: string, source = "record3d") => request<CaptureSet>(`/projects/${projectId}/capture-sets`, { method: "POST", body: JSON.stringify({ name, source }) }),
    getSet: (projectId: string, id: string) => request<CaptureSet & { frames?: CaptureFrame[] }>(`/projects/${projectId}/capture-sets/${id}`),
    captureFrame: (projectId: string, setId: string) => request<CaptureFrame>(`/projects/${projectId}/capture-sets/${setId}/frames:capture`, { method: "POST", body: JSON.stringify({ count: 1 }) }),
    importFrames: async (projectId: string, setId: string, files: File[]) => {
      const bundle = await readFrameBundles(files);
      const value = await request<{ items: CaptureFrame[]; count: number; capture_set: CaptureSet }>(`/projects/${projectId}/capture-sets/${setId}/frames:import`, { method: "POST", body: JSON.stringify(bundle) });
      return { accepted: value.items.filter((item) => item.included).length, rejected: value.items.filter((item) => !item.included).length, capture_set: value.capture_set };
    },
    setFrameIncluded: (projectId: string, setId: string, frameId: string, included: boolean) => request<CaptureFrame>(`/projects/${projectId}/capture-sets/${setId}/frames/${frameId}`, { method: "PATCH", body: JSON.stringify({ included }) }),
  },
  maps: {
    list: (projectId: string, signal?: AbortSignal) => request<SceneMap[]>(`/projects/${projectId}/maps`, { signal }),
    create: (projectId: string, set: CaptureSet, computeProfile: string, name: string) => request<SceneMap & { job_id: string }>(`/projects/${projectId}/maps`, { method: "POST", body: JSON.stringify({ capture_set_id: set.id, capture_set_revision: set.revision, compute_profile: computeProfile, name }) }),
    saveTransform: (projectId: string, id: string, transform: MapTransform) => request<{ status: string; user_transform: MapTransform }>(`/projects/${projectId}/maps/${id}/transform`, { method: "POST", body: JSON.stringify(transform) }),
    activate: (projectId: string, id: string) => request<void>(`/projects/${projectId}/maps/${id}/activate`, { method: "POST" }),
    generateMesh: (projectId: string, id: string, openmvsBin?: string) => request<{ job_id: string }>(`/projects/${projectId}/maps/${id}/mesh`, { method: "POST", body: JSON.stringify({ openmvs_bin: openmvsBin || null }) }),
    manifest: (projectId: string, id: string, signal?: AbortSignal) => request<PointCloudManifest>(`/projects/${projectId}/maps/${id}/point-cloud/manifest`, { signal }),
  },
  probe: {
    list: (projectId: string, signal?: AbortSignal) => request<ProbeCalibration[]>(`/projects/${projectId}/probe-calibrations`, { signal }),
    validate: (projectId: string, file: File) => upload<CalibrationValidation>(`/projects/${projectId}/probe-calibrations/validate`, file),
    import: (projectId: string, validationId: string, activate = true) => request<ProbeCalibration>(`/projects/${projectId}/probe-calibrations/import`, { method: "POST", body: JSON.stringify({ validation_id: validationId, activate }) }),
    activate: (projectId: string, id: string) => request<void>(`/projects/${projectId}/probe-calibrations/${id}/activate`, { method: "POST" }),
    createRevision: (projectId: string, id: string, blob: ProbeCalibration["blob_detector"]) => request<ProbeCalibration>(`/projects/${projectId}/probe-calibrations/${id}/revisions`, { method: "POST", body: JSON.stringify({ blob_detector: blob, activate: true }) }),
    download: (projectId: string, id: string) => downloadFromApi(`/projects/${projectId}/probe-calibrations/${id}/download`, "probe_calibration.json"),
  },
  registration: {
    list: (projectId: string, signal?: AbortSignal) => request<Registration[]>(`/projects/${projectId}/registrations`, { signal }),
    create: (projectId: string, mapId: string, calibrationId: string) => request<Registration>(`/projects/${projectId}/registrations`, { method: "POST", body: JSON.stringify({ map_id: mapId, probe_calibration_id: calibrationId, name: "Metric board registration" }) }),
    addObservation: (projectId: string, id: string) => request<Registration>(`/projects/${projectId}/registrations/${id}/observations`, { method: "POST", body: JSON.stringify({ source: "current_frame" }) }),
    solve: (projectId: string, id: string) => request<Registration>(`/projects/${projectId}/registrations/${id}/solve`, { method: "POST" }),
    validate: (projectId: string, id: string, acceptWarning = false) => request<Registration>(`/projects/${projectId}/registrations/${id}/validate`, { method: "POST", body: JSON.stringify({ accept_warning: acceptWarning, note: acceptWarning ? "Operator accepted the measured residual warning after inspection." : undefined }) }),
    activate: (projectId: string, id: string) => request<Registration>(`/projects/${projectId}/registrations/${id}/activate`, { method: "POST" }),
  },
  sessions: {
    list: (projectId: string, signal?: AbortSignal) => request<SessionSummary[]>(`/projects/${projectId}/sessions`, { signal }),
    create: (projectId: string, name: string) => request<SessionSnapshot>(`/projects/${projectId}/sessions`, { method: "POST", body: JSON.stringify({ name }) }),
    get: (projectId: string, id: string, signal?: AbortSignal) => request<SessionSnapshot>(`/projects/${projectId}/sessions/${id}`, { signal }),
    lifecycle: (projectId: string, id: string, action: "start" | "pause" | "resume" | "stop" | "finalize") => request<SessionSnapshot>(`/projects/${projectId}/sessions/${id}/${action}`, { method: "POST" }),
    trackingSnapshot: (projectId: string, id: string) => request<SessionSnapshot>(`/projects/${projectId}/sessions/${id}/tracking-snapshot`),
    records: (projectId: string, id: string, filters: ReviewFilters, cursor?: string, signal?: AbortSignal) => request<PagedResult<PaintedRecord>>(`/projects/${projectId}/sessions/${id}/painted-records${reviewQueryString(filters, cursor)}`, { signal }),
    points: (projectId: string, id: string, filters: ReviewFilters, cursor?: string, signal?: AbortSignal) => request<PagedResult<PaintedRecord>>(`/projects/${projectId}/sessions/${id}/painted-points${reviewQueryString({ ...filters, type: "point" }, cursor)}`, { signal }),
    paths: (projectId: string, id: string, filters: ReviewFilters, cursor?: string, signal?: AbortSignal) => request<PagedResult<PaintedRecord>>(`/projects/${projectId}/sessions/${id}/painted-paths${reviewQueryString({ ...filters, type: "path" }, cursor)}`, { signal }),
    updateRecord: (projectId: string, sessionId: string, record: PaintedRecord, note: string) => request<PaintedRecord>(`/projects/${projectId}/sessions/${sessionId}/painted-${record.type}s/${record.id}`, { method: "PATCH", body: JSON.stringify({ note }) }),
    deleteRecord: (projectId: string, sessionId: string, record: PaintedRecord) => request<void>(`/projects/${projectId}/sessions/${sessionId}/painted-${record.type}s/${record.id}`, { method: "DELETE" }),
    restoreRecord: (projectId: string, sessionId: string, record: PaintedRecord) => request<PaintedRecord>(`/projects/${projectId}/sessions/${sessionId}/painted-${record.type}s/${record.id}/restore`, { method: "POST" }),
    replay: (projectId: string, id: string, from: number, to: number) => request<{ records: PaintedRecord[] }>(`/projects/${projectId}/sessions/${id}/replay${queryString({ from, to })}`),
  },
  exports: {
    list: (projectId: string, sessionId: string) => request<ExportSnapshot[]>(`/projects/${projectId}/sessions/${sessionId}/exports`),
    create: (projectId: string, sessionId: string, format: ExportSnapshot["format"], filters: ReviewFilters) => request<ExportSnapshot>(`/projects/${projectId}/sessions/${sessionId}/exports`, { method: "POST", body: JSON.stringify({ format, filters: normalizeReviewFiltersForApi(filters), include_deleted: filters.include_deleted }) }),
    download: (projectId: string, sessionId: string, id: string) => downloadFromApi(`/projects/${projectId}/sessions/${sessionId}/exports/${id}/download`),
  },
  jobs: {
    get: (id: string) => request<JobSnapshot>(`/jobs/${id}`), cancel: (id: string) => request<JobSnapshot>(`/jobs/${id}/cancel`, { method: "POST" }), resume: (id: string) => request<JobSnapshot>(`/jobs/${id}/resume`, { method: "POST" }),
  },
};

export function unwrapList<T>(value: PagedResult<T> | T[]): T[] { return Array.isArray(value) ? value : value.items; }
export function errorMessage(value: unknown): string { if (value instanceof ApiError) return [value.message, value.suggestedAction].filter(Boolean).join(" "); return value instanceof Error ? value.message : "An unexpected error occurred."; }
