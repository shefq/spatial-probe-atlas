import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { ViewerEngine } from "../engine/ViewerEngineV1";
import type { CameraItem, MapTransformData, PaintDataDelta, RegistrationView, TransformMode, ViewerFilters, ViewerMetrics, ViewerMode, ViewerSelection } from "../engine/types";
import type { TrackingViewFrame } from "../../api/types";
import { api } from "../../api/client";
import { useUiStore } from "../../stores";
import { Button, InlineAlert } from "../../components/ui";

export interface SpatialViewerProps {
  mode: ViewerMode;
  projectId: string;
  mapId: string;
  sessionId?: string;
  selection?: ViewerSelection;
  filters?: ViewerFilters;
  registration?: RegistrationView;
  probeGeometry?: number[][];
  cameraIntrinsics?: { matrix: number[]; width: number; height: number; };
  paintData?: PaintDataDelta;
  onSelectionChange?: (value: ViewerSelection) => void;
  onMetrics?: (value: ViewerMetrics) => void;
  className?: string;
}
export interface SpatialViewerHandle {
  applyTrackingFrame: (frame: TrackingViewFrame) => void;
  setPaintData: (delta: PaintDataDelta) => void;
  setRegistration: (registration: RegistrationView) => void;
  resetView: () => void;
  getMetrics: () => ViewerMetrics | null;
  setTransformMode: (mode: TransformMode) => void;
  getMapTransform: () => MapTransformData | null;
  resetMapTransform: () => void;
  setPointSize: (size: number) => void;
  setCamSize: (size: number) => void;
  loadMesh: (projectId: string, mapId: string) => Promise<void>;
  setMeshVisibility: (visible: boolean) => void;
  reloadMap: () => void;
}

export const SpatialViewer = forwardRef<SpatialViewerHandle, SpatialViewerProps>(function SpatialViewerV1(
  { mode, projectId, mapId, selection, filters, registration, probeGeometry, cameraIntrinsics, paintData, onMetrics, className = "" }, ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<ViewerEngine | null>(null);
  const metricsRef = useRef(onMetrics); metricsRef.current = onMetrics;
  const pushToast = useUiStore((state) => state.pushToast);
  const viewMode = useUiStore((state) => state.viewMode);
  const setViewMode = useUiStore((state) => state.setViewMode);
  const [engineReady, setEngineReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const [transformMode, setTransformModeState] = useState<TransformMode>("none");
  const [pointSize, setPointSizeState] = useState<number>(0.012);
  const [camSize, setCamSizeState] = useState<number>(0.08);
  const [framePopup, setFramePopup] = useState<{ cam: CameraItem; imgUrl: string } | null>(null);

  const handleCameraDoubleClick = useCallback((cam: CameraItem) => {
    if (!cam.name && !cam.frame_id) return;
    const imgUrl = cam.frame_id
      ? `/api/v1/projects/${projectId}/frames/by-id/${cam.frame_id}/image`
      : `/api/v1/projects/${projectId}/frames/${encodeURIComponent(cam.name!)}/image`;
    setFramePopup({ cam, imgUrl });
  }, [projectId]);

  useImperativeHandle(ref, () => ({
    applyTrackingFrame: (frame) => engineRef.current?.applyTrackingFrame(frame),
    setPaintData: (delta) => engineRef.current?.setPaintData(delta),
    setRegistration: (value) => engineRef.current?.setRegistration(value),
    resetView: () => engineRef.current?.resetView(),
    getMetrics: () => engineRef.current?.getMetrics() ?? null,
    setTransformMode: (m) => { setTransformModeState(m); engineRef.current?.setTransformMode(m); },
    getMapTransform: () => engineRef.current?.getMapTransform() ?? null,
    resetMapTransform: () => engineRef.current?.resetMapTransform(),
    setPointSize: (sz) => { setPointSizeState(sz); engineRef.current?.setPointSize(sz); },
    setCamSize: (sz) => { setCamSizeState(sz); engineRef.current?.setCamSize(sz); },
    loadMesh: async (projectId: string, mapId: string) => await engineRef.current?.loadMesh(projectId, mapId),
    setMeshVisibility: (visible: boolean) => engineRef.current?.setMeshVisibility(visible),
    reloadMap: () => setGeneration((g) => g + 1),
  }), []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let cancelled = false;
    let nextFrame = 0;
    let observer: ResizeObserver | null = null;
    const engine = new ViewerEngine();
    setEngineReady(false); setLoading(true); setError(null);
    const start = async () => {
      const rect = container.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1) { nextFrame = requestAnimationFrame(start); return; }
      try {
        await engine.initialize(container, { mode, pointBudget: filters?.pointBudget });
        if (cancelled) { engine.dispose(); return; }
        engine.onCameraSelect = (cam) => {
          pushToast({
            kind: "info",
            title: `📷 ${cam.name ?? "Camera Selected"}`,
            message: `Pose Position: [${cam.position.map((n) => n.toFixed(3)).join(", ")}]`,
          });
        };
        engine.onCameraDoubleClick = handleCameraDoubleClick;
        engineRef.current = engine; setEngineReady(true);
        observer = new ResizeObserver((entries) => { const size = entries[0]?.contentRect; if (size) engine.resize(size.width, size.height, window.devicePixelRatio || 1); });
        observer.observe(container);
      } catch (value) { if (!cancelled) { setError(value instanceof Error ? value.message : "The 3D viewer could not start."); setLoading(false); } }
    };
    void start();
    const metricsTimer = window.setInterval(() => { if (!document.hidden && engineRef.current) metricsRef.current?.(engineRef.current.getMetrics()); }, 500);
    return () => { cancelled = true; cancelAnimationFrame(nextFrame); clearInterval(metricsTimer); observer?.disconnect(); if (engineRef.current === engine) engineRef.current = null; engine.dispose(); };
  }, [generation]);

  // Keep handler current if projectId changes without re-mounting
  useEffect(() => {
    if (engineRef.current) engineRef.current.onCameraDoubleClick = handleCameraDoubleClick;
  }, [handleCameraDoubleClick]);

  useEffect(() => {
    if (!engineReady || !engineRef.current || !projectId || !mapId) { if (engineReady) setLoading(false); return; }
    let active = true; setLoading(true); setError(null);
    engineRef.current.loadMap({
      projectId, mapId,
      manifestUrl: `/api/v1/projects/${projectId}/maps/${mapId}/point-cloud/manifest`,
      tileUrl: (tileId) => `/api/v1/projects/${projectId}/maps/${mapId}/point-cloud/tiles/${encodeURIComponent(tileId)}`,
    }).catch((value) => { if (active) setError(value instanceof Error ? value.message : "The point cloud could not be loaded."); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [engineReady, projectId, mapId, generation]);

  useEffect(() => { if (engineReady && mode) engineRef.current?.setMode(mode); }, [engineReady, mode]);
  useEffect(() => {
    if (engineReady && filters) {
      engineRef.current?.setFilters({
        ...filters,
        ...(viewMode === "mesh" ? { showPoints: false } : {}),
      });
    }
  }, [engineReady, filters, viewMode]);
  useEffect(() => { if (engineReady && selection) engineRef.current?.setSelection(selection); }, [engineReady, selection]);
  useEffect(() => { if (engineReady && registration) engineRef.current?.setRegistration(registration); }, [engineReady, registration]);
  useEffect(() => { if (engineReady && probeGeometry) engineRef.current?.setProbeGeometry(probeGeometry); }, [engineReady, probeGeometry]);
  useEffect(() => { if (engineReady && cameraIntrinsics) engineRef.current?.setCameraIntrinsics(cameraIntrinsics); }, [engineReady, cameraIntrinsics]);
  useEffect(() => { if (engineReady && paintData) engineRef.current?.setPaintData(paintData); }, [engineReady, paintData]);

  useEffect(() => {
    if (!engineReady || !engineRef.current || !projectId || !mapId) return;
    if (viewMode === "mesh") {
      engineRef.current.loadMesh(projectId, mapId).catch(console.error);
      engineRef.current.setMeshVisibility(true);
      engineRef.current.setFilters({ showPoints: false });
    } else {
      engineRef.current.setMeshVisibility(false);
      engineRef.current.setFilters({ showPoints: true });
    }
  }, [engineReady, viewMode, projectId, mapId]);

  // Close popup on Escape key
  useEffect(() => {
    if (!framePopup) return;
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") setFramePopup(null); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [framePopup]);

  const handleSetTransformMode = (m: TransformMode) => {
    setTransformModeState(m);
    engineRef.current?.setTransformMode(m);
  };

  const handlePointChange = (size: number) => {
    const val = Math.max(0.001, Math.min(0.15, size));
    setPointSizeState(val);
    engineRef.current?.setPointSize(val);
  };

  const handleCamSizeChange = (size: number) => {
    const val = Math.max(0.01, Math.min(0.5, size));
    setCamSizeState(val);
    engineRef.current?.setCamSize(val);
  };

  const handleSaveTransform = async () => {
    if (!engineRef.current || !projectId || !mapId) return;
    const transform = engineRef.current.getMapTransform();
    try {
      await api.maps.saveTransform(projectId, mapId, transform);
      pushToast({ kind: "success", title: "Map transform saved", message: `Scale: ${transform.scale.toFixed(4)}` });
    } catch {
      pushToast({ kind: "error", title: "Could not save map transform" });
    }
  };

  const handleResetTransform = () => {
    engineRef.current?.resetMapTransform();
    pushToast({ kind: "success", title: "Map transform reset" });
  };

  return (
    <div className={`spatial-viewer ${className}`} data-mode={mode} data-testid={`viewer-${mode}`}>
      <div className="spatial-viewer__canvas" ref={containerRef} />
      <div className="spatial-viewer__chrome">
        <div className="spatial-viewer__toolbar" style={{ display: "flex", gap: "12px", alignItems: "center", background: "rgba(10, 15, 24, 0.85)", padding: "6px 12px", borderRadius: "8px", backdropFilter: "blur(8px)", border: "1px solid rgba(255, 255, 255, 0.1)" }}>
          <span className="viewer-mode">{mode}</span>

          <div style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "12px", borderLeft: "1px solid rgba(255,255,255,0.15)", paddingLeft: "10px" }} title="Adjust point cloud point size">
            <span>Pt Size:</span>
            <input type="range" min="0.001" max="0.15" step="0.001" value={pointSize}
              onChange={(e) => handlePointChange(parseFloat(e.target.value))}
              style={{ width: "60px", accentColor: "#58d6ff" }} />
            <span style={{ fontFamily: "monospace", minWidth: "40px" }}>{(pointSize * 1000).toFixed(1)}mm</span>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "12px", borderLeft: "1px solid rgba(255,255,255,0.15)", paddingLeft: "10px" }} title="Adjust camera pyramid rendering size">
            <span>Cam Size:</span>
            <input type="range" min="0.02" max="0.5" step="0.005" value={camSize}
              onChange={(e) => handleCamSizeChange(parseFloat(e.target.value))}
              style={{ width: "60px", accentColor: "#ffea00" }} />
            <span style={{ fontFamily: "monospace", minWidth: "40px" }}>{(camSize * 1000).toFixed(0)}mm</span>
          </div>

          {mode === "mapping" || mode === "registration" ? (
            <div style={{ display: "flex", alignItems: "center", gap: "6px", borderLeft: "1px solid rgba(255,255,255,0.15)", paddingLeft: "10px" }} title="Interactive Map Transform (Hotkeys: T, R, S, Esc)">
              <span style={{ fontSize: "12px" }}>Transform:</span>
              <div style={{ display: "flex", gap: "3px" }}>
                <Button size="sm" variant={transformMode === "translate" ? "primary" : "ghost"} onClick={() => handleSetTransformMode("translate")}>Move (T)</Button>
                <Button size="sm" variant={transformMode === "rotate" ? "primary" : "ghost"} onClick={() => handleSetTransformMode("rotate")}>Rotate (R)</Button>
                <Button size="sm" variant={transformMode === "scale" ? "primary" : "ghost"} onClick={() => handleSetTransformMode("scale")}>Scale (S)</Button>
                <Button size="sm" variant={transformMode === "none" ? "default" : "ghost"} onClick={() => handleSetTransformMode("none")}>Off</Button>
              </div>
              {transformMode !== "none" ? (
                <div style={{ display: "flex", gap: "3px", marginLeft: "4px" }}>
                  <Button size="sm" variant="primary" onClick={handleSaveTransform}>Save</Button>
                  <Button size="sm" variant="ghost" onClick={handleResetTransform}>Reset</Button>
                </div>
              ) : null}
            </div>
          ) : null}

          <Button variant="ghost" size="sm" onClick={() => engineRef.current?.resetView()}>Reset view</Button>
        </div>
      </div>

      {loading ? <div className="viewer-loading"><span className="spinner" /> Loading spatial data…</div> : null}
      {error ? <div className="viewer-error"><InlineAlert tone="danger" title="3D view unavailable" action={<Button size="sm" onClick={() => setGeneration((v) => v + 1)}>Retry</Button>}>{error}</InlineAlert></div> : null}

      {/* Camera frame image lightbox — triggered by double-clicking a pyramid */}
      {framePopup ? (
        <div
          onClick={() => setFramePopup(null)}
          style={{
            position: "absolute", inset: 0, zIndex: 100,
            background: "rgba(0,0,0,0.88)",
            display: "flex", alignItems: "center", justifyContent: "center",
            backdropFilter: "blur(6px)",
            cursor: "zoom-out",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: "rgba(10,16,28,0.97)",
              border: "1px solid rgba(255,234,0,0.3)",
              borderRadius: "12px",
              padding: "16px",
              maxWidth: "90vw",
              maxHeight: "92vh",
              display: "flex",
              flexDirection: "column",
              gap: "12px",
              boxShadow: "0 24px 64px rgba(0,0,0,0.75)",
              cursor: "default",
            }}
          >
            {/* Header */}
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "24px" }}>
              <div>
                <div style={{ fontSize: "14px", fontWeight: 700, color: "#ffea00", display: "flex", alignItems: "center", gap: "6px" }}>
                  📷 {framePopup.cam.name}
                </div>
                <div style={{ fontSize: "11px", color: "#667788", marginTop: "3px" }}>
                  Position: [{framePopup.cam.position.map((n) => n.toFixed(4)).join(", ")}]
                </div>
              </div>
              <button
                onClick={() => setFramePopup(null)}
                style={{
                  background: "rgba(255,255,255,0.07)", border: "1px solid rgba(255,255,255,0.15)",
                  color: "#aabbcc", borderRadius: "6px", padding: "5px 12px",
                  cursor: "pointer", fontSize: "12px", whiteSpace: "nowrap",
                }}
              >✕ Close (Esc)</button>
            </div>

            {/* Image */}
            <img
              src={framePopup.imgUrl}
              alt={framePopup.cam.name ?? "Camera frame"}
              style={{
                maxWidth: "80vw",
                maxHeight: "78vh",
                objectFit: "contain",
                borderRadius: "8px",
                border: "1px solid rgba(255,255,255,0.07)",
                background: "#050a12",
              }}
              onError={(e) => {
                const img = e.target as HTMLImageElement;
                img.style.display = "none";
                const msg = document.createElement("div");
                msg.textContent = "⚠ Image not available";
                msg.style.cssText = "color:#ff7479;padding:32px;font-size:13px";
                img.parentNode?.insertBefore(msg, img.nextSibling);
              }}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
});
