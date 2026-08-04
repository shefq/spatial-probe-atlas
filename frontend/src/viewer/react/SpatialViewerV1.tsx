import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { ViewerEngine } from "../engine/ViewerEngineV1";
import type { PaintDataDelta, RegistrationView, ViewerFilters, ViewerMetrics, ViewerMode, ViewerSelection } from "../engine/types";
import type { TrackingViewFrame } from "../../api/types";
import { Button, InlineAlert } from "../../components/ui";

export interface SpatialViewerProps {
  mode: ViewerMode;
  projectId: string;
  mapId: string;
  sessionId?: string;
  selection?: ViewerSelection;
  filters?: ViewerFilters;
  registration?: RegistrationView;
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
}

export const SpatialViewer = forwardRef<SpatialViewerHandle, SpatialViewerProps>(function SpatialViewerV1(
  { mode, projectId, mapId, selection, filters, registration, paintData, onMetrics, className = "" }, ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<ViewerEngine | null>(null);
  const metricsRef = useRef(onMetrics); metricsRef.current = onMetrics;
  const [engineReady, setEngineReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);

  useImperativeHandle(ref, () => ({
    applyTrackingFrame: (frame) => engineRef.current?.applyTrackingFrame(frame),
    setPaintData: (delta) => engineRef.current?.setPaintData(delta),
    setRegistration: (value) => engineRef.current?.setRegistration(value),
    resetView: () => engineRef.current?.resetView(),
    getMetrics: () => engineRef.current?.getMetrics() ?? null,
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
        engineRef.current = engine; setEngineReady(true);
        observer = new ResizeObserver((entries) => { const size = entries[0]?.contentRect; if (size) engine.resize(size.width, size.height, window.devicePixelRatio || 1); });
        observer.observe(container);
      } catch (value) { if (!cancelled) { setError(value instanceof Error ? value.message : "The 3D viewer could not start."); setLoading(false); } }
    };
    void start();
    const metricsTimer = window.setInterval(() => { if (!document.hidden && engineRef.current) metricsRef.current?.(engineRef.current.getMetrics()); }, 500);
    return () => { cancelled = true; cancelAnimationFrame(nextFrame); clearInterval(metricsTimer); observer?.disconnect(); if (engineRef.current === engine) engineRef.current = null; engine.dispose(); };
  }, [generation]);

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

  useEffect(() => { engineRef.current?.setMode(mode); }, [mode]);
  useEffect(() => { if (filters) engineRef.current?.setFilters(filters); }, [filters]);
  useEffect(() => { if (selection) engineRef.current?.setSelection(selection); }, [selection]);
  useEffect(() => { if (registration) engineRef.current?.setRegistration(registration); }, [registration]);
  useEffect(() => { if (paintData) engineRef.current?.setPaintData(paintData); }, [paintData]);

  return <div className={`spatial-viewer ${className}`} data-mode={mode} data-testid={`viewer-${mode}`}>
    <div className="spatial-viewer__canvas" ref={containerRef} />
    <div className="spatial-viewer__chrome"><span className="viewer-mode">{mode}</span><Button variant="ghost" size="sm" onClick={() => engineRef.current?.resetView()}>Reset view</Button></div>
    {loading ? <div className="viewer-loading"><span className="spinner" /> Loading spatial data…</div> : null}
    {error ? <div className="viewer-error"><InlineAlert tone="danger" title="3D view unavailable" action={<Button size="sm" onClick={() => setGeneration((value) => value + 1)}>Retry</Button>}>{error}</InlineAlert></div> : null}
  </div>;
});
