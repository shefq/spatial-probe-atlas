import { describe, expect, it } from "vitest";
import type { PointCloudManifest, PointCloudTileDescriptor } from "../api/types";
import { decodePointTile, selectTiles } from "../viewer/point-cloud/lod";

const tile = (id: string, points: number, error: number, x: number): PointCloudTileDescriptor => ({
  id,
  url: `/tiles/${id}`,
  bounds: [x, 0, 0, x + 1, 1, 1],
  point_count: points,
  geometric_error: error,
});

describe("point-cloud LOD", () => {
  it("stays inside the budget while preferring high screen-space error", () => {
    const manifest: PointCloudManifest = {
      schema_version: "1",
      map_id: "map",
      point_count: 300,
      bounds: [0, 0, 0, 3, 1, 1],
      root_tiles: ["a"],
      tiles: { a: tile("a", 100, 10, 0), b: tile("b", 100, 2, 1), c: tile("c", 100, 1, 2) },
    };
    const selected = selectTiles(manifest, [0, 0, 2], 200);
    expect(selected.map((value) => value.id)).toContain("a");
    expect(selected.reduce((sum, value) => sum + value.point_count, 0)).toBeLessThanOrEqual(200);
  });

  it("decodes quantized local positions and RGB", () => {
    const descriptor = tile("a", 1, 1, 0);
    const buffer = new ArrayBuffer(9);
    const view = new DataView(buffer);
    view.setUint16(0, 65535, true); view.setUint16(2, 0, true); view.setUint16(4, 32768, true);
    view.setUint8(6, 10); view.setUint8(7, 20); view.setUint8(8, 30);
    const value = decodePointTile(buffer, descriptor);
    expect(Array.from(value.colors)).toEqual([10, 20, 30]);
    expect(value.positions[0]).toBeCloseTo(1);
    expect(value.positions[2]).toBeCloseTo(0.5, 3);
  });
});
