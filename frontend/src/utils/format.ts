const RESERVED_WINDOWS_NAMES = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;

export function validateProjectName(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return "Enter a project name.";
  if (trimmed.length > 80) return "Use 80 characters or fewer.";
  if (CONTROL_CHARACTERS.test(trimmed)) return "Control characters are not allowed.";
  if (RESERVED_WINDOWS_NAMES.test(trimmed)) return "This name is reserved by Windows.";
  if (/[. ]$/.test(trimmed)) return "A project name cannot end with a period or space.";
  return null;
}

export function formatBytes(value?: number | null): string {
  if (value === undefined || value === null || !Number.isFinite(value)) return "—";
  if (value === 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(Math.floor(Math.log(Math.abs(value)) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

export function formatDuration(seconds?: number | null): string {
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds)) return "—";
  const value = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const remainder = value % 60;
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}` : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

export function formatCount(value?: number | null): string {
  return value === undefined || value === null ? "—" : new Intl.NumberFormat().format(value);
}

export function formatDate(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function formatCoordinate(value: number | undefined, units: "mm" | "m" = "mm"): string {
  if (value === undefined || !Number.isFinite(value)) return "—";
  return units === "mm" ? `${(value * 1000).toFixed(1)} mm` : `${value.toFixed(4)} m`;
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
