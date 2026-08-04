import { afterEach, describe, expect, it, vi } from "vitest";
import { api, normalizeReviewFiltersForApi } from "../api/client";
import type { ReviewFilters } from "../api/types";


afterEach(() => vi.unstubAllGlobals());


describe("review API contract", () => {
  it("normalizes datetime-local bounds to UTC without changing the filter meaning", () => {
    const filters: ReviewFilters = {
      type: "point",
      quality: "low",
      include_deleted: true,
      from: "2026-08-04T10:15",
      to: "2026-08-04T10:45",
    };
    expect(normalizeReviewFiltersForApi(filters)).toEqual({
      ...filters,
      from: new Date(filters.from!).toISOString(),
      to: new Date(filters.to!).toISOString(),
    });
    expect(normalizeReviewFiltersForApi({ type: "all", quality: "all", include_deleted: false })).toEqual({
      type: "all", quality: "all", include_deleted: false, from: undefined, to: undefined,
    });
  });

  it("sends the complete filter snapshot and opaque cursor to the combined records route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], next_cursor: null, total: 0 }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;
    const filters: ReviewFilters = {
      type: "path",
      quality: "warning",
      include_deleted: true,
      from: "2026-08-04T10:15",
      to: "2026-08-04T10:45",
    };
    await api.sessions.records("project-a", "session-a", filters, "opaque-cursor", signal);

    const [rawUrl, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const url = new URL(rawUrl, "http://localhost");
    expect(url.pathname).toBe("/api/v1/projects/project-a/sessions/session-a/painted-records");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      cursor: "opaque-cursor",
      type: "path",
      quality: "warning",
      from: new Date(filters.from!).toISOString(),
      to: new Date(filters.to!).toISOString(),
      include_deleted: "true",
    });
    expect(init.signal).toBe(signal);
  });

  it("freezes normalized UTC filters in the export request body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "export-a", state: "queued" }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const filters: ReviewFilters = {
      type: "all",
      quality: "good",
      include_deleted: false,
      from: "2026-08-04T09:00",
      to: "2026-08-04T10:00",
    };
    await api.exports.create("project-a", "session-a", "session_manifest", filters);

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      format: "session_manifest",
      filters: {
        ...filters,
        from: new Date(filters.from!).toISOString(),
        to: new Date(filters.to!).toISOString(),
      },
      include_deleted: false,
    });
  });
});
