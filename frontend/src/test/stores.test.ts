import { beforeEach, describe, expect, it } from "vitest";
import { useCameraStore, useLiveSessionStore, useReviewStore, useUiStore } from "../stores";

describe("workflow stores", () => {
  beforeEach(() => {
    useCameraStore.setState({ status: { state: "disconnected" }, devices: [], previewDropped: 0 });
    useLiveSessionStore.getState().reset();
    useReviewStore.getState().reset();
    useUiStore.setState({ draftScopes: {}, toasts: [] });
  });

  it("updates only camera summaries", () => {
    useCameraStore.getState().patchStatus({ state: "ready", fps: 22.4, frames_received: 5 });
    expect(useCameraStore.getState().status).toMatchObject({ state: "ready", fps: 22.4 });
    expect(useCameraStore.getState()).not.toHaveProperty("rawFrame");
  });

  it("tracks draft scopes without domain artifacts", () => {
    useUiStore.getState().setDraftDirty("probe-tuning", true);
    expect(useUiStore.getState().draftScopes["probe-tuning"]).toBe(true);
  });

  it("resets review selection and paging", () => {
    useReviewStore.getState().setSelectedId("point-1");
    useReviewStore.getState().setPaging("cursor-2", 30);
    useReviewStore.getState().reset();
    expect(useReviewStore.getState()).toMatchObject({ selectedId: null, cursor: null, total: 0 });
  });
});
