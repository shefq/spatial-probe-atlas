import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_BLOB_SETTINGS, type ProbeCalibration } from "../api/types";

const { createRevision } = vi.hoisted(() => ({ createRevision: vi.fn() }));
vi.mock("../api/client", () => ({
  api: { probe: { createRevision } },
  errorMessage: (value: unknown) => value instanceof Error ? value.message : String(value),
}));
vi.mock("../api/streams", () => ({
  ReconnectingSocket: class {
    connect() {}
    close() {}
    send() { return "command"; }
  },
}));

import { BlobDetectorTuningModal } from "../features/probe/BlobDetectorTuningModal";

const calibration: ProbeCalibration = {
  id: "cal-1",
  project_id: "project-1",
  name: "Probe",
  schema_version: "1.0.0",
  state: "active",
  active: true,
  revision: 1,
  units: "m",
  probe: { model: "polaris_5_blob", marker_frame: "M", tip_frame: "P", marker_points_m: [[0,0,0],[1,0,0],[0,1,0],[0,0,1],[1,1,1]], t_marker_tip: [1,0,0,0,0,1,0,0,0,0,1,-.1,0,0,0,1] },
  blob_detector: DEFAULT_BLOB_SETTINGS,
  quality: { input_frame_count: 20, accepted_frame_count: 18, rms_reprojection_error_px: .8 },
};

describe("BlobDetectorTuningModal", () => {
  beforeEach(() => createRevision.mockReset());
  afterEach(cleanup);

  it("does not save a dirty draft when cancel is discarded", async () => {
    const onClose = vi.fn();
    render(<BlobDetectorTuningModal open projectId="project-1" calibration={calibration} onSaved={vi.fn()} onClose={onClose} />);
    fireEvent.change(screen.getByTestId("blob-minThreshold"), { target: { value: "70" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByText("Unsaved detector settings")).toBeVisible();
    fireEvent.click(screen.getByTestId("discard-blob-draft"));
    expect(createRevision).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("creates and activates a revision only when Save is chosen", async () => {
    const saved = { ...calibration, id: "cal-2", revision: 2, blob_detector: { ...DEFAULT_BLOB_SETTINGS, minThreshold: 70 } };
    createRevision.mockResolvedValue(saved);
    const onSaved = vi.fn();
    render(<BlobDetectorTuningModal open projectId="project-1" calibration={calibration} onSaved={onSaved} onClose={vi.fn()} />);
    fireEvent.change(screen.getByTestId("blob-minThreshold"), { target: { value: "70" } });
    fireEvent.click(screen.getByRole("button", { name: "Save to current project" }));
    await waitFor(() => expect(createRevision).toHaveBeenCalledWith("project-1", "cal-1", expect.objectContaining({ minThreshold: 70 })));
    expect(onSaved).toHaveBeenCalledWith(saved);
  });
});
