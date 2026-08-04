import { useDiagnosticsStore } from "../stores";
import { formatBytes } from "../utils/format";
import { InlineAlert, Metric } from "./ui";

export function ResourceStatus({ compact = false }: { compact?: boolean }) {
  const resources = useDiagnosticsStore((state) => state.resources);
  if (!resources) return compact ? <span className="muted">Resources —</span> : null;
  if (compact) {
    return (
      <div className="resource-compact" title="Local resource snapshot">
        <span>RAM {resources.ram_used_percent?.toFixed(0) ?? "—"}%</span>
        <span>Disk {formatBytes(resources.disk_free_bytes)} free</span>
      </div>
    );
  }
  return (
    <div>
      {resources.warnings.map((warning, index) => (
        <InlineAlert key={warning.id ?? `${warning.code}-${index}`} tone={warning.severity === "critical" ? "danger" : "warning"} title={warning.code.replaceAll("_", " ")}>
          {warning.message} {warning.suggested_action}
        </InlineAlert>
      ))}
      <div className="metric-grid metric-grid--resources">
        <Metric label="CPU" value={resources.cpu_percent === undefined ? "—" : `${resources.cpu_percent.toFixed(0)}%`} />
        <Metric label="RAM" value={resources.ram_used_percent === undefined ? "—" : `${resources.ram_used_percent.toFixed(0)}%`} tone={(resources.ram_used_percent ?? 0) > 85 ? "warning" : undefined} />
        <Metric label="Disk free" value={formatBytes(resources.disk_free_bytes)} tone={(resources.disk_free_bytes ?? Infinity) < 20 * 1024 ** 3 ? "warning" : undefined} />
        <Metric label="VRAM" value={resources.vram_used_percent == null ? "CPU mode" : `${resources.vram_used_percent.toFixed(0)}%`} />
      </div>
    </div>
  );
}
