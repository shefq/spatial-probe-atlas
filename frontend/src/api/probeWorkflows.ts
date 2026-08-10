import type { ProbeCalibration } from "./types";

export interface ProbeCapture {
  id: string;
  project_id: string;
  state: string;
  input_frame_count: number;
  accepted_frame_count: number;
  frame_count?: number;
}

async function json<T>(path: string, body?: Record<string, unknown>): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { error?: { message?: string; suggested_action?: string } } | null;
    throw new Error([payload?.error?.message ?? `Request failed (${response.status}).`, payload?.error?.suggested_action].filter(Boolean).join(" "));
  }
  return response.json() as Promise<T>;
}

export const probeWorkflowApi = {
  createCapture: (projectId: string) => json<ProbeCapture>(`/projects/${projectId}/probe-captures`, { source: "camera" }),
  captureFrame: (projectId: string, captureId: string, calibrationId?: string) =>
    json<ProbeCapture>(`/projects/${projectId}/probe-captures/${captureId}/frames:capture`, calibrationId ? { calibration_id: calibrationId } : undefined),
  createCalibration: (projectId: string, captureId: string, name: string, acceptWarning = false) =>
    json<ProbeCalibration>(`/projects/${projectId}/probe-calibrations`, { probe_capture_id: captureId, name, accept_warning: acceptWarning }),
  arucoCapture: (projectId: string, captureId?: string, markerIds?: number[]) =>
    json<{ probe_capture: ProbeCapture }>(`/projects/${projectId}/aruco-calibrations/capture`, { capture_id: captureId, marker_ids: markerIds }),
  arucoSolve: (projectId: string, captureId: string, markerIds?: number[], anchorId?: number, activate = true) =>
    json<{ probe_calibration: ProbeCalibration; registration: any }>(`/projects/${projectId}/aruco-calibrations/solve`, { probe_capture_id: captureId, markerIds, anchorId, activate }),
  arucoAlignMap: (projectId: string, mapId: string, captureId?: string, markerIds?: number[], nominalMarkerSizeM?: number) =>
    json<any>(`/projects/${projectId}/maps/${mapId}/align-aruco`, { probe_capture_id: captureId || undefined, marker_ids: markerIds, nominal_marker_size_m: nominalMarkerSizeM }),
};
