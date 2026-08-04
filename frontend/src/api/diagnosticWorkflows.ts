export interface LogLine {
  timestamp: string;
  level: string;
  component: string;
  event: string;
  message?: string;
  trace_id?: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { ...init, credentials: "same-origin", headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  if (!response.ok) throw new Error(`Diagnostic request failed (${response.status}).`);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const diagnosticWorkflowApi = {
  logTail: () => request<{ items: LogLine[] }>("/system/logs/tail?limit=100"),
  revealLogs: () => request<void>("/system/logs/reveal", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() } }),
  migrateDataRoot: (path: string) => request<{ job_id: string }>("/system/data-root-migrations", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ destination: path }) }),
};
