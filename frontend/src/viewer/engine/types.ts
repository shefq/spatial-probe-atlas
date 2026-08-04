import type { PaintedRecord, PointCloudManifest, TrackingViewFrame } from "../../api/types";

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

export interface ViewerEngine {
  initialize(container: HTMLElement, options: ViewerOptions): Promise<void>;
  setMode(mode: ViewerMode): void;
  loadMap(source: PointCloudSource): Promise<void>;
  setRegistration(value: RegistrationView): void;
  applyTrackingFrame(value: TrackingViewFrame): void;
  setPaintData(value: PaintDataDelta): void;
  setFilters(value: ViewerFilters): void;
  setSelection(value: ViewerSelection): void;
  resize(width: number, height: number, dpr: number): void;
  getMetrics(): ViewerMetrics;
  resetView(): void;
  dispose(): void;
}
