import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { ExportSnapshot, PaintedRecord, ReviewFilters, SessionSnapshot } from "../api/types";
import { SpatialViewer } from "../viewer/react/SpatialViewer";
import { Button, Card, EmptyState, Field, InlineAlert, Metric, Modal, Segmented, Skeleton, StatusBadge, TextInput, Toggle } from "../components/ui";
import { useReviewStore, useUiStore } from "../stores";
import { formatBytes, formatCoordinate, formatCount, formatDate, formatDuration } from "../utils/format";
import { ManualAnnotationModal } from "../components/ManualAnnotationModal";

const exportFormats: Array<{ value: ExportSnapshot["format"]; label: string; detail: string }> = [
  { value: "json", label: "JSON data", detail: "Points, paths, frames, units, quality and filter snapshot." },
  { value: "csv", label: "CSV tables", detail: "Tabular points plus flattened path samples." },
  { value: "session_manifest", label: "Session manifest", detail: "Revision references, counts, checksums and app version." },
  { value: "screenshot", label: "Orthographic review image", detail: "Deterministic top-down W-XY paint view; not the browser camera." },
  { value: "point_overlay", label: "Point overlay", detail: "Point/path overlay export; no mesh or GLB." },
];

export function SessionReviewPage() {
  const { projectId = "", sessionId = "" } = useParams();
  const filters = useReviewStore((state) => state.filters);
  const setFilters = useReviewStore((state) => state.setFilters);
  const records = useReviewStore((state) => state.records);
  const setRecords = useReviewStore((state) => state.setRecords);
  const cursor = useReviewStore((state) => state.cursor);
  const total = useReviewStore((state) => state.total);
  const setPaging = useReviewStore((state) => state.setPaging);
  const replaceRecord = useReviewStore((state) => state.replaceRecord);
  const selectedId = useReviewStore((state) => state.selectedId);
  const setSelectedId = useReviewStore((state) => state.setSelectedId);
  const replayTime = useReviewStore((state) => state.replayTime);
  const replayPlaying = useReviewStore((state) => state.replayPlaying);
  const setReplay = useReviewStore((state) => state.setReplay);
  const resetReview = useReviewStore((state) => state.reset);
  const units = useUiStore((state) => state.displayUnits);
  const pushToast = useUiStore((state) => state.pushToast);
  const [session, setSession] = useState<SessionSnapshot | null>(null);
  const [exports, setExports] = useState<ExportSnapshot[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingRecords, setLoadingRecords] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportFormat, setExportFormat] = useState<ExportSnapshot["format"]>("json");
  const [noteDraft, setNoteDraft] = useState("");
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [annotateRecord, setAnnotateRecord] = useState<PaintedRecord | null>(null);

  useEffect(() => {
    resetReview();
    const controller = new AbortController();
    setLoading(true); setError(null);
    api.sessions.get(projectId, sessionId, controller.signal).then((value) => {
      setSession(value); setLoading(false); return api.exports.list(projectId, sessionId).catch(() => []);
    }).then((exportValues) => setExports(exportValues)).catch((value) => { if (!controller.signal.aborted) { setError(errorMessage(value)); setLoading(false); } });
    return () => controller.abort();
  }, [projectId, sessionId]);

  useEffect(() => {
    if (!replayPlaying || !session) return;
    const timer = window.setInterval(() => {
      const next = replayTime + 0.25;
      if (next >= (session.duration_seconds ?? 0)) setReplay(0, false);
      else setReplay(next);
    }, 250);
    return () => window.clearInterval(timer);
  }, [replayPlaying, replayTime, session]);

  const filtered = useMemo(() => records.filter((record) => {
    const time = record.type === "point" ? record.timestamp : record.started_at;
    return (filters.type === "all" || record.type === filters.type)
      && (filters.quality === "all" || record.quality === filters.quality || (filters.quality === "low" && record.quality === "flagged_low_quality"))
      && (filters.include_deleted || !record.deleted)
      && (!filters.from || new Date(time) >= new Date(filters.from))
      && (!filters.to || new Date(time) <= new Date(filters.to));
  }), [filters, records]);
  const visibleAtReplay = replayPlaying || replayTime > 0 ? filtered.filter((record) => session?.started_at && (recordTime(record) - new Date(session.started_at).valueOf()) / 1000 <= replayTime) : filtered;
  const selected = records.find((record) => record.id === selectedId) ?? null;
  useEffect(() => { setNoteDraft(selected?.note ?? ""); }, [selectedId, selected?.note]);
  const readOnly = ["running", "paused", "degraded", "stopping"].includes(session?.state ?? "");
  const invalidRange = Boolean(filters.from && filters.to && new Date(filters.from) > new Date(filters.to));

  useEffect(() => {
    const controller = new AbortController();
    if (invalidRange) {
      setRecords([], false);
      setPaging(null, 0);
      setLoadingRecords(false);
      return () => controller.abort();
    }
    setLoadingRecords(true);
    setSelectedId(null);
    api.sessions.records(projectId, sessionId, filters, undefined, controller.signal)
      .then((page) => {
        setRecords(page.items, false);
        setPaging(page.next_cursor ?? null, page.total ?? page.items.length);
      })
      .catch((value) => { if (!controller.signal.aborted) setError(errorMessage(value)); })
      .finally(() => { if (!controller.signal.aborted) setLoadingRecords(false); });
    return () => controller.abort();
  }, [projectId, sessionId, filters, invalidRange, setPaging, setRecords, setSelectedId]);

  async function loadMoreRecords() {
    if (!cursor) return;
    setLoadingRecords(true);
    try {
      const page = await api.sessions.records(projectId, sessionId, filters, cursor);
      setRecords(page.items, true);
      setPaging(page.next_cursor ?? null, page.total ?? total);
    } catch (value) { setError(errorMessage(value)); }
    finally { setLoadingRecords(false); }
  }

  const saveAnnotation = async () => {
    if (!selected || noteDraft.length > 1000) return;
    setBusy(true);
    try { const updated = await api.sessions.updateRecord(projectId, sessionId, selected, noteDraft.trim()); replaceRecord(updated); pushToast({ kind: "success", title: "Annotation saved" }); }
    catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const toggleDelete = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      if (selected.deleted) replaceRecord(await api.sessions.restoreRecord(projectId, sessionId, selected));
      else { await api.sessions.deleteRecord(projectId, sessionId, selected); replaceRecord({ ...selected, deleted: true }); }
      pushToast({ kind: "success", title: selected.deleted ? "Record restored" : "Record soft-deleted" });
    } catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const createExport = async () => {
    if (invalidRange) return;
    setBusy(true);
    try { const created = await api.exports.create(projectId, sessionId, exportFormat, filters); setExports((items) => [created, ...items]); setExportOpen(false); pushToast({ kind: "success", title: "Export job created", message: "Its immutable filter snapshot and checksum will be recorded." }); }
    catch (value) { setError(errorMessage(value)); }
    finally { setBusy(false); }
  };
  const compareToggle = (id: string) => setCompareIds((current) => current.includes(id) ? current.filter((value) => value !== id) : [...current.slice(-1), id]);

  if (loading) return <div className="page"><Skeleton lines={10} /></div>;
  if (!session) return <div className="page"><InlineAlert tone="danger" title="Session unavailable">{error ?? "The session could not be found."}</InlineAlert></div>;
  return (
    <div className="page page--workflow page--review">
      <header className="page-heading"><div><div className="eyebrow">STEP 5 · INSPECTION</div><h1>{session.name}</h1><p>Review persisted map-frame records, annotate without rewriting coordinates, and create reproducible exports.</p></div><div className="page-heading__actions"><StatusBadge state={session.state} /><Button variant="primary" onClick={() => setExportOpen(true)}>Export session</Button></div></header>
      {readOnly ? <InlineAlert tone="info" title="Active session — read-only review">Live acquisition can be inspected here, but annotations and deletion are disabled until it stops.</InlineAlert> : null}
      {error ? <InlineAlert tone="danger" title="Review action failed" action={<Button size="sm" onClick={() => setError(null)}>Dismiss</Button>}>{error} Corrupt artifacts are isolated; the source session is not rewritten.</InlineAlert> : null}
      <div className="status-card-row"><Metric label="Duration" value={formatDuration(session.duration_seconds)} /><Metric label="Session size" value={formatBytes(session.size_bytes)} /><Metric label="Frames" value={formatCount(session.frame_count)} /><Metric label="Tracked ratio" value={session.tracked_ratio == null ? "—" : `${(session.tracked_ratio * 100).toFixed(1)}%`} /><Metric label="Points" value={formatCount(session.point_count)} /><Metric label="Paths" value={formatCount(session.path_count)} /></div>
      <div className="review-layout">
        <aside className="review-filters">
          <Card title="Filters & legend" eyebrow={`${total} MATCHING`}>
            <Field label="Record type"><select className="select" value={filters.type} onChange={(event) => setFilters({ type: event.target.value as ReviewFilters["type"] })}><option value="all">Points & paths</option><option value="point">Points only</option><option value="path">Paths only</option></select></Field>
            <Field label="Quality"><select className="select" value={filters.quality} onChange={(event) => setFilters({ quality: event.target.value as ReviewFilters["quality"] })}><option value="all">All quality</option><option value="good">Good</option><option value="warning">Warning</option><option value="low">Low / overridden</option></select></Field>
            <div className="field-grid"><Field label="From" error={invalidRange ? "Start must precede end." : undefined}><input className="input" type="datetime-local" value={filters.from ?? ""} onChange={(event) => setFilters({ from: event.target.value || undefined })} /></Field><Field label="To"><input className="input" type="datetime-local" value={filters.to ?? ""} onChange={(event) => setFilters({ to: event.target.value || undefined })} /></Field></div>
            <Toggle label="Show soft-deleted records" checked={filters.include_deleted} onChange={(event) => setFilters({ include_deleted: event.target.checked })} />
            <div className="review-legend"><span><i className="legend-dot legend-dot--good" /> Good</span><span><i className="legend-dot legend-dot--warning" /> Warning</span><span><i className="legend-dot legend-dot--low" /> Low</span><span><i className="legend-line" /> Path</span></div>
            <Button onClick={() => setFilters({ type: "all", quality: "all", include_deleted: false, from: undefined, to: undefined })}>Reset filters</Button>
          </Card>
          <Card title="Selected record" eyebrow={selected?.type.toUpperCase() ?? "NONE"}>
            {selected ? <><SelectedSummary record={selected} units={units} onAnnotate={setAnnotateRecord} /><Field label="Annotation" error={noteDraft.length > 1000 ? "Use 1,000 characters or fewer." : undefined}><textarea className="textarea" value={noteDraft} maxLength={1001} disabled={readOnly} onChange={(event) => setNoteDraft(event.target.value)} /></Field><div className="button-row"><Button variant="primary" busy={busy} disabled={readOnly || noteDraft === (selected.note ?? "") || noteDraft.length > 1000} onClick={() => void saveAnnotation()}>Save note</Button><Button variant={selected.deleted ? "default" : "danger"} busy={busy} disabled={readOnly} onClick={() => void toggleDelete()}>{selected.deleted ? "Restore" : "Soft delete"}</Button><Button onClick={() => compareToggle(selected.id)}>{compareIds.includes(selected.id) ? "Remove compare" : "Compare"}</Button></div></> : <p className="muted">Choose a point or path from the viewer or table.</p>}
          </Card>
          {compareIds.length === 2 ? <CompareCard records={records.filter((record) => compareIds.includes(record.id))} units={units} /> : null}
        </aside>
        <div className="review-main">
          <Card className="viewer-card review-viewer-card" title="Session atlas" eyebrow="PROGRESSIVE POINT CLOUD + PAINT" actions={<span className="muted">World frame W · stored in metres</span>}>
            {session.map_id ? <SpatialViewer mode="review" projectId={projectId} mapId={session.map_id} sessionId={sessionId} paintData={{ reset: true, upsert: visibleAtReplay }} filters={{ includeDeleted: filters.include_deleted, quality: filters.quality, showPoints: filters.type !== "path", showPaths: filters.type !== "point" }} selection={selected ? { kind: selected.type, id: selected.id, position: selected.type === "point" ? selected.position_w_m : selected.positions_w_m[0] } : { kind: "none" }} /> : <EmptyState title="Map revision unavailable">The paint table remains reviewable. Use repair/reindex diagnostics to relink the immutable session map artifact.</EmptyState>}
          </Card>
          <Card title="Timeline replay" eyebrow={replayPlaying ? "PLAYING" : "PAUSED"}><div className="timeline"><Button size="sm" onClick={() => setReplay(replayTime, !replayPlaying)}>{replayPlaying ? "Ⅱ" : "▶"}</Button><input type="range" min={0} max={session.duration_seconds ?? 0} step={0.1} value={replayTime} onChange={(event) => setReplay(Number(event.target.value), false)} aria-label="Replay time" /><span>{formatDuration(replayTime)} / {formatDuration(session.duration_seconds)}</span></div></Card>
          <Card title="Persisted records" eyebrow="SERVER FILTERED · PAGED"><RecordTable records={filtered} selectedId={selectedId} units={units} compareIds={compareIds} onSelect={setSelectedId} onCompare={compareToggle} />{cursor ? <Button busy={loadingRecords} onClick={() => void loadMoreRecords()}>Load more records</Button> : null}</Card>
          <Card title="Exports" eyebrow="CHECKSUMMED ARTIFACTS" actions={<Button size="sm" onClick={() => setExportOpen(true)}>New export</Button>}>
            {exports.length ? <div className="export-list">{exports.map((item) => <div key={item.id}><span><strong>{item.format.replaceAll("_", " ")}</strong><small>{formatDate(item.created_at)} · {formatBytes(item.size_bytes)}{item.checksum_sha256 ? ` · ${item.checksum_sha256.slice(0, 12)}…` : ""}</small></span><StatusBadge state={item.state} />{item.state === "completed" ? <Button size="sm" onClick={() => api.exports.download(projectId, sessionId, item.id)}>Download</Button> : null}</div>)}</div> : <p className="muted">No exports yet. Each export records schema, units, frames, filter snapshot and SHA-256 checksum.</p>}
          </Card>
        </div>
      </div>
      <Modal open={exportOpen} title="Create reproducible export" description="The selected format and current filters are frozen into the export manifest." onRequestClose={() => setExportOpen(false)} size="md" footer={<><Button onClick={() => setExportOpen(false)}>Cancel</Button><Button variant="primary" busy={busy} disabled={invalidRange} onClick={() => void createExport()}>Create export job</Button></>}>
        <div className="export-format-list" role="radiogroup" aria-label="Export format">{exportFormats.map((format) => <label className={exportFormat === format.value ? "is-selected" : ""} key={format.value}><input type="radio" name="export-format" checked={exportFormat === format.value} onChange={() => setExportFormat(format.value)} /><span><strong>{format.label}</strong><small>{format.detail}</small></span></label>)}</div>
        <InlineAlert tone="info" title="Current filter snapshot">{filters.type} · {filters.quality} quality · {filters.include_deleted ? "including" : "excluding"} deleted records. Coordinates remain in frame W and metres.</InlineAlert>
      </Modal>
      <ManualAnnotationModal open={!!annotateRecord} projectId={projectId} sessionId={sessionId} record={annotateRecord} onClose={() => setAnnotateRecord(null)} onSuccess={(updated) => { setAnnotateRecord(null); replaceRecord(updated); if (selectedId === updated.id) setSelectedId(updated.id); }} />
    </div>
  );
}

function recordTime(record: PaintedRecord): number { return new Date(record.type === "point" ? record.timestamp : record.started_at).valueOf(); }

function SelectedSummary({ record, units, onAnnotate }: { record: PaintedRecord; units: "mm" | "m"; onAnnotate: (record: PaintedRecord) => void }) {
  return <div className="selected-summary"><div><small>ID</small><code>{record.id.slice(0, 12)}…</code></div><div><small>Timestamp</small><strong>{formatDate(record.type === "point" ? record.timestamp : record.started_at)}</strong></div><div><small>Quality</small><StatusBadge state={record.quality} /></div>{record.type === "point" ? <div><small>Position W</small><strong>{record.position_w_m ? record.position_w_m.map((value) => formatCoordinate(value, units)).join(" · ") : <div style={{ display: "flex", gap: "8px", alignItems: "center" }}><span style={{ color: "#f2bd55" }}>Needs Annotation</span><Button size="sm" onClick={() => onAnnotate(record)}>Annotate</Button></div>}</strong></div> : <><div><small>Samples</small><strong>{formatCount(record.sample_count)}</strong></div><div><small>Length</small><strong>{formatCoordinate(record.length_m, units)}</strong></div></>}</div>;
}

function CompareCard({ records, units }: { records: PaintedRecord[]; units: "mm" | "m" }) {
  const r1 = records[0]; const r2 = records[1];
  const distance = records.length === 2 && r1.type === "point" && r2.type === "point" && r1.position_w_m && r2.position_w_m ? Math.sqrt((r1.position_w_m[0] - r2.position_w_m[0]) ** 2 + (r1.position_w_m[1] - r2.position_w_m[1]) ** 2 + (r1.position_w_m[2] - r2.position_w_m[2]) ** 2) : undefined;
  return <Card title="Comparison" eyebrow="2 RECORDS"><Metric label="Time delta" value={records.length === 2 ? formatDuration(Math.abs(recordTime(records[1]) - recordTime(records[0])) / 1000) : "—"} /><Metric label="Point distance" value={formatCoordinate(distance, units)} /></Card>;
}

function RecordTable({ records, selectedId, units, compareIds, onSelect, onCompare }: { records: PaintedRecord[]; selectedId: string | null; units: "mm" | "m"; compareIds: string[]; onSelect: (id: string) => void; onCompare: (id: string) => void }) {
  if (!records.length) return <EmptyState icon="·" title="No records match these filters">A valid empty session is still exportable. Adjust filters or resume acquisition if the session is recoverable.</EmptyState>;
  return <div className="data-table data-table--review"><div className="data-table__head"><span>Time</span><span>Type</span><span>Position / samples</span><span>Quality</span><span>Note</span><span>Compare</span></div>{records.map((record) => <button className={`data-table__row ${selectedId === record.id ? "is-selected" : ""} ${record.deleted ? "is-deleted" : ""}`} key={record.id} onClick={() => onSelect(record.id)}><span>{formatDate(record.type === "point" ? record.timestamp : record.started_at)}</span><span>{record.type}</span><span>{record.type === "point" ? (record.position_w_m ? record.position_w_m.map((value) => formatCoordinate(value, units)).join(" · ") : "Needs Annotation") : `${formatCount(record.sample_count)} samples`}</span><span><StatusBadge state={record.quality} /></span><span>{record.note ?? "—"}</span><span><input type="checkbox" checked={compareIds.includes(record.id)} onClick={(event) => event.stopPropagation()} onChange={() => onCompare(record.id)} aria-label={`Compare ${record.id}`} /></span></button>)}</div>;
}
