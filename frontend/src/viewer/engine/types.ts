import type { PaintedRecord, PointCloudManifest, TrackingViewFrame } from "../../api/types";
import type { MarkerItem } from "../point-cloud/v1";

export type ViewerMode = "mapping" | "registration" | "live" | "review";

export interface ViewerOptions {
  mode: ViewerMode;
  pointBudget?: number;
  background?: number;
  targetFrameTimeMs?: number;
}

export interface PointCloudSource {
  projectId: string;
  mapId: string;
  manifestUrl: string;
  tileUrl?: (tileId: string) => string;
  manifest?: PointCloudManifest;
}

export interface RegistrationView {
  t_w_b?: number[];
  scale?: number;
  residualPoints?: Array<{ from: [number, number, number]; to: [number, number, number]; errorMm: number }>;
  observations?: Array<[number, number, number]>;
  board_definition?: any;
}

export interface PaintDataDelta {
  reset?: boolean;
  upsert?: PaintedRecord[];
  removeIds?: string[];
  provisional?: Array<{ id: string; position: [number, number, number]; quality: string }>;
}

export interface ViewerFilters {
  showMap?: boolean;
  showFrames?: boolean;
  showProbe?: boolean;
  showBoard?: boolean;
  showMarkers?: boolean;
  showPoints?: boolean;
  showPaths?: boolean;
  includeDeleted?: boolean;
  quality?: string;
  pointSize?: number;
  pointBudget?: number;
}

export interface ViewerSelection {
  kind: "none" | "point" | "path" | "map_point" | "observation";
  id?: string;
  position?: [number, number, number];
}

export interface ViewerMetrics {
  visiblePoints: number;
  loadedPoints: number;
  loadedTiles: number;
  drawCalls: number;
  frameTimeMs: number;
  pixelRatio: number;
  contextLost: boolean;
}

export type TransformMode = "translate" | "rotate" | "scale" | "none";

export interface MapTransformData {
  position: [number, number, number];
  quaternion: [number, number, number, number];
  scale: number;
}

export interface CameraItem {
  id?: string;
  name?: string;
  frame_id?: string;
  position: [number, number, number];
  quaternion?: [number, number, number, number];
}

export interface ViewerEngine {
  onCameraDoubleClick?: ((cam: CameraItem) => void) | null;
  initialize(container: HTMLElement, options: ViewerOptions): Promise<void>;
  setMode(mode: ViewerMode): void;
  loadMap(source: PointCloudSource): Promise<void>;
  setRegistration(value: RegistrationView): void;
  setProbeGeometry(geometry: number[][]): void;
  applyTrackingFrame(value: TrackingViewFrame): void;
  setPaintData(value: PaintDataDelta): void;
  setFilters(value: ViewerFilters): void;
  setSelection(value: ViewerSelection): void;
  setTransformMode(mode: TransformMode): void;
  getMapTransform(): MapTransformData;
  resetMapTransform(): void;
  setCameras(cameras: CameraItem[]): void;
  setMarkers(markers: MarkerItem[]): void;
  setCamSize(size: number): void;
  setPointSize(size: number): void;
  loadMesh(projectId: string, mapId: string): Promise<void>;
  setMeshVisibility(visible: boolean): void;
  resize(width: number, height: number, dpr: number): void;
  getMetrics(): ViewerMetrics;
  resetView(): void;
  dispose(): void;
}
