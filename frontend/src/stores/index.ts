import { create } from "zustand";
import type {
  AppSettings,
  CameraDevice,
  CameraStatus,
  Capabilities,
  DiagnosticCheck,
  JobSnapshot,
  PaintedRecord,
  Project,
  ProjectSummary,
  ResourceSnapshot,
  ReviewFilters,
  SceneMap,
  SessionSnapshot,
  TrackingViewFrame,
} from "../api/types";

export interface ToastMessage {
  id: string;
  kind: "success" | "info" | "warning" | "error";
  title: string;
  message?: string;
}

interface UiState {
  theme: "dark";
  displayUnits: "mm" | "m";
  sidePanelCollapsed: boolean;
  draftScopes: Record<string, boolean>;
  toasts: ToastMessage[];
  setDisplayUnits: (units: "mm" | "m") => void;
  toggleSidePanel: () => void;
  setDraftDirty: (scope: string, dirty: boolean) => void;
  pushToast: (toast: Omit<ToastMessage, "id">) => string;
  dismissToast: (id: string) => void;
  viewMode: "points" | "mesh";
  setViewMode: (mode: "points" | "mesh") => void;
}

export const useUiStore = create<UiState>((set) => ({
  theme: "dark",
  displayUnits: "mm",
  sidePanelCollapsed: false,
  draftScopes: {},
  toasts: [],
  setDisplayUnits: (displayUnits) => set({ displayUnits }),
  toggleSidePanel: () => set((state) => ({ sidePanelCollapsed: !state.sidePanelCollapsed })),
  setDraftDirty: (scope, dirty) => set((state) => ({ draftScopes: { ...state.draftScopes, [scope]: dirty } })),
  pushToast: (toast) => {
    const id = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
    set((state) => ({ toasts: [...state.toasts, { ...toast, id }].slice(-5) }));
    return id;
  },
  dismissToast: (id) => set((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) })),
  viewMode: "points",
  setViewMode: (viewMode) => set({ viewMode }),
}));

interface ProjectState {
  projects: Project[];
  activeProject: ProjectSummary | null;
  activeMap: SceneMap | null;
  loading: boolean;
  error: string | null;
  setProjects: (projects: Project[]) => void;
  setActiveProject: (project: ProjectSummary | null) => void;
  setActiveMap: (map: SceneMap | null) => void;
  updateProject: (project: Project) => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
}

export const useProjectStore = create<ProjectState>((set) => ({
  projects: [],
  activeProject: null,
  activeMap: null,
  loading: false,
  error: null,
  setProjects: (projects) => set({ projects }),
  setActiveProject: (activeProject) => set({ activeProject }),
  setActiveMap: (activeMap) => set({ activeMap }),
  updateProject: (project) =>
    set((state) => ({
      projects: state.projects.map((current) => (current.id === project.id ? { ...current, ...project } : current)),
      activeProject: state.activeProject?.id === project.id ? { ...state.activeProject, ...project } : state.activeProject,
    })),
  setLoading: (loading) => set({ loading }),
  setError: (error) => set({ error }),
}));

interface CameraState {
  devices: CameraDevice[];
  status: CameraStatus;
  previewMode: "rgb" | "depth" | "split";
  previewQuality: "low" | "medium" | "high";
  streamState: "closed" | "connecting" | "open" | "reconnecting";
  previewDropped: number;
  setDevices: (devices: CameraDevice[]) => void;
  setStatus: (status: CameraStatus) => void;
  patchStatus: (status: Partial<CameraStatus>) => void;
  setPreviewMode: (mode: CameraState["previewMode"]) => void;
  setPreviewQuality: (quality: CameraState["previewQuality"]) => void;
  setStreamState: (state: CameraState["streamState"]) => void;
  setPreviewDropped: (count: number) => void;
}

export const useCameraStore = create<CameraState>((set) => ({
  devices: [],
  status: { state: "disconnected" },
  previewMode: "rgb",
  previewQuality: "medium",
  streamState: "closed",
  previewDropped: 0,
  setDevices: (devices) => set({ devices }),
  setStatus: (status) => set({ status }),
  patchStatus: (patch) => set((state) => ({ status: { ...state.status, ...patch } })),
  setPreviewMode: (previewMode) => set({ previewMode }),
  setPreviewQuality: (previewQuality) => set({ previewQuality }),
  setStreamState: (streamState) => set({ streamState }),
  setPreviewDropped: (previewDropped) => set({ previewDropped }),
}));

interface JobStateStore {
  jobs: Record<string, JobSnapshot>;
  upsert: (job: JobSnapshot) => void;
  remove: (id: string) => void;
}

export const useJobStore = create<JobStateStore>((set) => ({
  jobs: {},
  upsert: (job) => set((state) => ({ jobs: { ...state.jobs, [job.id]: { ...state.jobs[job.id], ...job } } })),
  remove: (id) =>
    set((state) => {
      const jobs = { ...state.jobs };
      delete jobs[id];
      return { jobs };
    }),
}));

interface LiveSessionState {
  session: SessionSnapshot | null;
  paintingMode: "point" | "path";
  samplingMode: "time" | "distance";
  sampleIntervalMs: number;
  sampleDistanceMm: number;
  trackingSummary: TrackingViewFrame | null;
  reconnectState: "closed" | "connecting" | "open" | "reconnecting";
  pointCount: number;
  pathCount: number;
  provisionalCount: number;
  setSession: (session: SessionSnapshot | null) => void;
  setPaintingMode: (mode: "point" | "path") => void;
  setSamplingMode: (mode: "time" | "distance") => void;
  setSampling: (value: number) => void;
  setTrackingSummary: (summary: TrackingViewFrame | null) => void;
  setReconnectState: (state: LiveSessionState["reconnectState"]) => void;
  setCounts: (pointCount: number, pathCount: number) => void;
  changeProvisional: (delta: number) => void;
  reset: () => void;
}

export const useLiveSessionStore = create<LiveSessionState>((set) => ({
  session: null,
  paintingMode: "point",
  samplingMode: "distance",
  sampleIntervalMs: 100,
  sampleDistanceMm: 2,
  trackingSummary: null,
  reconnectState: "closed",
  pointCount: 0,
  pathCount: 0,
  provisionalCount: 0,
  setSession: (session) => set({ session }),
  setPaintingMode: (paintingMode) => set({ paintingMode }),
  setSamplingMode: (samplingMode) => set({ samplingMode }),
  setSampling: (value) => set((state) => (state.samplingMode === "time" ? { sampleIntervalMs: value } : { sampleDistanceMm: value })),
  setTrackingSummary: (trackingSummary) => set({ trackingSummary }),
  setReconnectState: (reconnectState) => set({ reconnectState }),
  setCounts: (pointCount, pathCount) => set({ pointCount, pathCount }),
  changeProvisional: (delta) => set((state) => ({ provisionalCount: Math.max(0, state.provisionalCount + delta) })),
  reset: () => set({ session: null, trackingSummary: null, reconnectState: "closed", pointCount: 0, pathCount: 0, provisionalCount: 0 }),
}));

interface ReviewState {
  filters: ReviewFilters;
  records: PaintedRecord[];
  selectedId: string | null;
  cursor: string | null;
  total: number;
  replayTime: number;
  replayPlaying: boolean;
  setFilters: (filters: Partial<ReviewFilters>) => void;
  setRecords: (records: PaintedRecord[], append?: boolean) => void;
  replaceRecord: (record: PaintedRecord) => void;
  setSelectedId: (id: string | null) => void;
  setPaging: (cursor: string | null, total?: number) => void;
  setReplay: (time: number, playing?: boolean) => void;
  reset: () => void;
}

const defaultReviewFilters: ReviewFilters = { type: "all", quality: "all", include_deleted: false };

export const useReviewStore = create<ReviewState>((set) => ({
  filters: defaultReviewFilters,
  records: [],
  selectedId: null,
  cursor: null,
  total: 0,
  replayTime: 0,
  replayPlaying: false,
  setFilters: (filters) => set((state) => ({ filters: { ...state.filters, ...filters }, cursor: null })),
  setRecords: (records, append = false) => set((state) => ({ records: append ? [...state.records, ...records] : records })),
  replaceRecord: (record) => set((state) => ({ records: state.records.map((item) => (item.id === record.id ? record : item)) })),
  setSelectedId: (selectedId) => set({ selectedId }),
  setPaging: (cursor, total) => set((state) => ({ cursor, total: total ?? state.total })),
  setReplay: (replayTime, replayPlaying) => set((state) => ({ replayTime, replayPlaying: replayPlaying ?? state.replayPlaying })),
  reset: () => set({ filters: defaultReviewFilters, records: [], selectedId: null, cursor: null, total: 0, replayTime: 0, replayPlaying: false }),
}));

interface DiagnosticsState {
  capabilities: Capabilities | null;
  resources: ResourceSnapshot | null;
  checks: DiagnosticCheck[];
  settings: AppSettings | null;
  loading: boolean;
  setCapabilities: (value: Capabilities) => void;
  setResources: (value: ResourceSnapshot) => void;
  setChecks: (value: DiagnosticCheck[]) => void;
  setSettings: (value: AppSettings) => void;
  setLoading: (value: boolean) => void;
}

export const useDiagnosticsStore = create<DiagnosticsState>((set) => ({
  capabilities: null,
  resources: null,
  checks: [],
  settings: null,
  loading: false,
  setCapabilities: (capabilities) => set({ capabilities }),
  setResources: (resources) => set({ resources }),
  setChecks: (checks) => set({ checks }),
  setSettings: (settings) => set({ settings }),
  setLoading: (loading) => set({ loading }),
}));

export const projectSelectors = {
  activeId: (state: ProjectState) => state.activeProject?.id ?? null,
  readiness: (state: ProjectState) => state.activeProject?.readiness,
};

export const cameraSelectors = {
  ready: (state: CameraState) => state.status.state === "ready",
  health: (state: CameraState) => ({
    fps: state.status.fps,
    latency_ms: state.status.latency_ms,
    dropped_frames: state.status.dropped_frames,
    incomplete_frames: state.status.incomplete_frames,
  }),
};
