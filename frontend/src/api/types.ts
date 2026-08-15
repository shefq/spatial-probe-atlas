export type UUID = string;
export type IsoTimestamp = string;
export type LifecycleState =
  | "draft"
  | "ready"
  | "active"
  | "archived"
  | "quarantined"
  | "failed"
  | "recoverable"
  | string;

export interface ApiErrorPayload {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
    trace_id?: string;
    retryable?: boolean;
    suggested_action?: string;
  };
}

export interface Project {
  id: UUID;
  name: string;
  state: LifecycleState;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
  revision: number;
  size_bytes?: number;
  capture_frame_count?: number;
  map_point_count?: number;
  session_count?: number;
  active_map_id?: UUID | null;
  active_probe_calibration_id?: UUID | null;
  active_registration_id?: UUID | null;
  readiness?: ProjectReadiness;
  warnings?: ResourceWarning[];
}

export interface ProjectReadiness {
  camera_ready: boolean;
  map_ready: boolean;
  probe_calibration_ready: boolean;
  registration_ready: boolean;
  storage_ready: boolean;
}

export interface ProjectSummary extends Project {
  sessions?: SessionSummary[];
  jobs?: JobSnapshot[];
}

export interface CameraDevice {
  device_id: string;
  adapter: "record3d" | "replay" | "external" | string;
  name: string;
  available: boolean;
  busy?: boolean;
  detail?: string;
}

export type CameraConnectionState =
  | "disconnected"
  | "enumerating"
  | "opening"
  | "waiting_for_frame"
  | "verifying"
  | "ready"
  | "degraded"
  | "error";

export interface CameraStatus {
  connection_id?: UUID | null;
  project_id?: UUID | null;
  state: CameraConnectionState;
  device?: CameraDevice | null;
  owner?: string | null;
  intrinsics_source?: "record3d_per_frame" | "external_calibration" | string;
  rgb_width?: number;
  rgb_height?: number;
  depth_width?: number;
  depth_height?: number;
  fps?: number;
  latency_ms?: number;
  frames_received?: number;
  dropped_frames?: number;
  incomplete_frames?: number;
  connected_seconds?: number;
  complete_frame_streak?: number;
  depth_aligned?: boolean;
  intrinsic_matrix?: number[];
  error?: string | null;
}

export interface CaptureSet {
  id: UUID;
  project_id: UUID;
  name: string;
  source: string;
  state: string;
  revision: number;
  frame_count: number;
  accepted_frame_count: number;
  excluded_frame_count: number;
  coverage?: number;
  size_bytes?: number;
  created_at: IsoTimestamp;
}

export interface CaptureFrame {
  id: UUID;
  sequence: number;
  included: boolean;
  thumbnail_url?: string;
  blur_score?: number;
  exposure_state?: string;
  quality?: "good" | "warning" | "rejected";
}

export interface MapTransform {
  position: [number, number, number];
  quaternion: [number, number, number, number];
  scale: number;
}

export interface SceneMap {
  id: UUID;
  project_id: UUID;
  name: string;
  state: string;
  active: boolean;
  capture_set_id: UUID;
  capture_set_revision: number;
  point_count?: number;
  registered_image_count?: number;
  reprojection_error_px?: number;
  units?: "arbitrary" | "m";
  size_bytes?: number;
  effective_compute_profile?: string;
  manifest_url?: string;
  user_transform?: MapTransform;
  job_id?: UUID;
  created_at: IsoTimestamp;
}

export type JobState =
  | "queued"
  | "admitted"
  | "processing"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed"
  | "interrupted"
  | "recoverable";

export interface JobSnapshot {
  id: UUID;
  type: string;
  state: JobState;
  stage?: string;
  stage_index?: number;
  stage_count?: number;
  progress?: number;
  message?: string;
  warnings?: string[];
  effective_compute_profile?: string;
  owner_project_id?: UUID;
  created_at?: IsoTimestamp;
  project_id?: UUID | null;
  owner_id?: UUID | null;
  target_project_id?: UUID;
  confirmation_recorded?: boolean;
  result?: {
    project_id?: UUID;
    project_name?: string;
    project?: Project;
    report?: { relative_uri: string; sha256: string; size_bytes: number };
    report_summary?: { recognized_file_count: number; unknown_file_count: number; warnings: number };
    [key: string]: unknown;
  };
  error?: {
    code: string;
    message?: string;
    details?: { defaulted_fields?: string[];[key: string]: unknown };
    retryable?: boolean;
    suggested_action?: string;
  } | null;
}

export interface PointCloudTileDescriptor {
  id: string;
  url: string;
  bounds: [number, number, number, number, number, number];
  point_count: number;
  geometric_error: number;
  children?: string[];
}

export interface PointCloudManifest {
  schema_version: string;
  map_id: UUID;
  point_count: number;
  bounds: [number, number, number, number, number, number];
  root_tiles: string[];
  tiles: Record<string, PointCloudTileDescriptor>;
  position_encoding?: "float32_xyz" | "quantized_uint16_xyz";
}

export interface BlobDetectorSettings {
  minThreshold: number;
  maxThreshold: number;
  thresholdStep: number;
  minRepeatability: number;
  minDistBetweenBlobs: number;
  maxReprojectionError?: number;
  filterByColor: boolean;
  blobColor: number;
  filterByArea: boolean;
  minArea: number;
  maxArea: number;
  filterByCircularity: boolean;
  minCircularity: number;
  maxCircularity: number;
  filterByInertia: boolean;
  minInertiaRatio: number;
  maxInertiaRatio: number;
  filterByConvexity: boolean;
  minConvexity: number;
  maxConvexity: number;
}

export const DEFAULT_BLOB_SETTINGS: BlobDetectorSettings = {
  minThreshold: 61,
  maxThreshold: 169,
  thresholdStep: 17,
  minRepeatability: 2,
  minDistBetweenBlobs: 10,
  maxReprojectionError: 2.5,
  filterByColor: true,
  blobColor: 0,
  filterByArea: true,
  minArea: 50,
  maxArea: 1261,
  filterByCircularity: true,
  minCircularity: 0.57,
  maxCircularity: 1,
  filterByInertia: true,
  minInertiaRatio: 0.1,
  maxInertiaRatio: 1,
  filterByConvexity: false,
  minConvexity: 0.87,
  maxConvexity: 1,
};

export interface ProbeCalibration {
  id: UUID;
  calibration_id?: UUID;
  project_id: UUID;
  name: string;
  schema_version: "1.0.0";
  state: string;
  active: boolean;
  revision: number;
  units: "m";
  probe: {
    model: "polaris_5_blob";
    marker_frame: "M";
    tip_frame: "P";
    marker_points_m: number[][];
    t_marker_tip: number[];
  };
  blob_detector: BlobDetectorSettings;
  quality: {
    input_frame_count: number;
    accepted_frame_count: number;
    rms_reprojection_error_px: number;
    max_reprojection_error_px?: number;
    notes?: string;
  };
  provenance?: Record<string, unknown>;
  created_at?: IsoTimestamp;
}

export interface ProbeTestMetrics {
  blob_count: number;
  candidate_count: number;
  inliers: number;
  tracked: boolean;
  reprojection_error_px?: number;
  rejection_reason?: string;
  exposure_feedback?: string;
  raw_image_url?: string;
  binary_image_url?: string;
  overlay_image_url?: string;
}

export interface CalibrationValidation {
  validation_id: UUID;
  valid: boolean;
  schema_version?: string;
  summary?: {
    marker_point_count?: number;
    units?: string;
    calibration_rms_px?: number;
  };
  warnings: string[];
  errors?: Array<{ path: string; message: string }>;
  expires_at?: IsoTimestamp;
}

export interface Registration {
  id: UUID;
  project_id: UUID;
  map_id: UUID;
  probe_calibration_id: UUID;
  name: string;
  state: string;
  active: boolean;
  scale?: number;
  rms_residual_mm?: number;
  max_residual_mm?: number;
  observation_count?: number;
  validation_state?: "pending" | "passed" | "accepted_with_warning" | "failed";
  t_w_b?: number[];
  board_definition?: any;
  created_at?: IsoTimestamp;
}

export type SessionState =
  | "draft"
  | "preflight"
  | "running"
  | "paused"
  | "degraded"
  | "stopping"
  | "stopped"
  | "finalized"
  | "failed"
  | "recoverable";

export interface SessionSummary {
  id: UUID;
  project_id: UUID;
  name: string;
  state: SessionState;
  started_at?: IsoTimestamp | null;
  ended_at?: IsoTimestamp | null;
  duration_seconds?: number;
  size_bytes?: number;
  frame_count?: number;
  point_count?: number;
  path_count?: number;
  tracked_ratio?: number;
  notes?: string;
  created_at: IsoTimestamp;
}

export interface PreflightCheck {
  key: string;
  label: string;
  passed: boolean;
  detail?: string;
  required_route?: string;
}

export interface SessionSnapshot extends SessionSummary {
  preflight?: PreflightCheck[];
  map_id?: UUID | null;
  probe_calibration_id?: UUID | null;
  registration_id?: UUID | null;
  recent_records?: PaintedRecord[];
  tracking?: TrackingViewFrame;
}

export interface TrackingViewFrame {
  session_id: UUID;
  frame_id: number;
  device_timestamp_ns: number;
  camera_state: "tracked" | "lost";
  probe_state: "tracked" | "lost";
  t_w_c?: number[];
  t_c_m?: number[];
  tip_w_m?: [number, number, number];
  camera_inliers?: number;
  camera_reprojection_error_px?: number;
  probe_inliers?: number;
  probe_reprojection_error_px?: number;
  fps?: number;
  latency_ms?: number;
  quality: "good" | "warning" | "low" | "lost";
}

export interface PaintedPoint {
  id: UUID;
  type: "point";
  session_id: UUID;
  timestamp: IsoTimestamp;
  position_w_m?: [number, number, number];
  quality: string;
  note?: string;
  label?: string;
  value?: number;
  color?: string;
  deleted?: boolean;
  metrics?: Record<string, number>;
  image_uri?: string;
}

export interface PaintedPath {
  id: UUID;
  type: "path";
  session_id: UUID;
  started_at: IsoTimestamp;
  ended_at?: IsoTimestamp;
  positions_w_m: Array<[number, number, number]>;
  sample_count: number;
  length_m?: number;
  quality: string;
  note?: string;
  deleted?: boolean;
}

export type PaintedRecord = PaintedPoint | PaintedPath;

export interface PagedResult<T> {
  items: T[];
  next_cursor?: string | null;
  total?: number;
}

export interface ReviewFilters {
  from?: string;
  to?: string;
  type: "all" | "point" | "path";
  quality: "all" | "good" | "warning" | "low";
  include_deleted: boolean;
}

export interface ExportSnapshot {
  id: UUID;
  session_id: UUID;
  format: "json" | "csv" | "session_manifest" | "screenshot" | "point_overlay";
  state: JobState;
  size_bytes?: number;
  checksum_sha256?: string;
  download_url?: string;
  job_id?: UUID;
  created_at?: IsoTimestamp;
}

export interface ResourceWarning {
  id?: string;
  severity: "info" | "warning" | "critical";
  code: string;
  message: string;
  suggested_action?: string;
}

export interface ResourceSnapshot {
  cpu_percent?: number;
  ram_used_percent?: number;
  ram_total_bytes?: number;
  disk_free_bytes?: number;
  disk_total_bytes?: number;
  vram_used_percent?: number | null;
  vram_total_bytes?: number | null;
  project_size_bytes?: number;
  warnings: ResourceWarning[];
  calculated_at?: IsoTimestamp;
}

export interface Capabilities {
  app_version: string;
  api_version?: string;
  schema_version?: string;
  compute_state: "cuda_ready" | "cuda_driver_only" | "cuda_incompatible" | "cpu_only" | "degraded";
  effective_compute_profile: string;
  cpu?: string;
  gpu?: string | null;
  cuda_version?: string | null;
  record3d_state?: string;
  replay_available?: boolean;
  data_root?: string;
  frontend_version?: string;
}

export interface DiagnosticCheck {
  key: string;
  name: string;
  state: "pass" | "warn" | "fail" | "skip" | "not_available";
  detail: string;
  impact?: string;
  fix?: string;
  checked_at?: IsoTimestamp;
}

export interface AppSettings {
  display_units: "mm" | "m";
  compute_profile: "auto" | "cpu" | "cuda";
  point_budget: number;
  decoded_cache_mib: number;
  continue_live_in_background: boolean;
  log_level?: string;
}

export interface WsEnvelope<T = unknown> {
  protocol_version: 1;
  type: string;
  seq: number;
  timestamp: IsoTimestamp;
  correlation_id?: string;
  data: T;
}
