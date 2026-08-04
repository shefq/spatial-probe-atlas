import { describe, expect, it } from "vitest";
import { decodeV1Tile, normalizeManifest } from "../viewer/point-cloud/v1";

describe("v1 octree transport", () => {
  it("normalizes the architecture manifest without exposing artifact paths", () => {
    const manifest = normalizeManifest({
      format: "spatial-probe-atlas-octree",
      version: 1,
      coordinate_frame: "W",
      units: "m",
      bounds: { min: [0, 0, 0], max: [1, 1, 1] },
      root_tiles: ["r"],
      tiles: { r: { tile_id: "r", uri: "projects/private/tiles/r.spatile", bounds: { min: [0, 0, 0], max: [1, 1, 1] }, point_count: 1, children: [], geometric_error_m: .1, sha256: "abc" } },
    });
    expect(manifest.tiles.r).toMatchObject({ id: "r", pointCount: 1, geometricErrorM: .1 });
    expect(manifest.coordinateFrame).toBe("W");
  });

  it("retains an immutable M0-to-W similarity for metric rendering", () => {
    const similarity = { scale: 0.25, rotation: [1, 0, 0, 0, 1, 0, 0, 0, 1], translation: [1, 2, 3] };
    const manifest = normalizeManifest({
      format: "spatial-probe-atlas-octree",
      version: 1,
      coordinate_frame: "M0",
      units: "arbitrary",
      similarity_s_w_m0: similarity,
      bounds: { min: [0, 0, 0], max: [1, 1, 1] },
      root_tiles: ["r"],
      tiles: { r: { tile_id: "r", bounds: { min: [0, 0, 0], max: [1, 1, 1] }, point_count: 1, children: [], geometric_error_m: .1 } },
    });
    expect(manifest.coordinateFrame).toBe("M0");
    expect(manifest.units).toBe("arbitrary");
    expect(manifest.similaritySWM0).toEqual(similarity);
  });

  it("decodes SPATILE1 little-endian quantized XYZ and RGB", () => {
    const buffer = new ArrayBuffer(49);
    const bytes = new Uint8Array(buffer);
    bytes.set(new TextEncoder().encode("SPATILE1"), 0);
    const view = new DataView(buffer);
    view.setUint16(8, 1, true); view.setUint16(10, 1, true); view.setUint32(12, 1, true);
    [0, 0, 0, 2, 4, 6].forEach((value, index) => view.setFloat32(16 + index * 4, value, true));
    view.setUint16(40, 32768, true); view.setUint16(42, 65535, true); view.setUint16(44, 0, true);
    view.setUint8(46, 10); view.setUint8(47, 20); view.setUint8(48, 30);
    const result = decodeV1Tile(buffer, { id: "r", uri: "ignored", bounds: { min: [0, 0, 0], max: [2, 4, 6] }, pointCount: 1, geometricErrorM: .1, children: [] });
    expect([...result.colors]).toEqual([10, 20, 30]);
    expect(result.positions[0]).toBeCloseTo(1, 3);
    expect(result.positions[1]).toBeCloseTo(4, 3);
    expect(result.positions[2]).toBe(0);
  });
});
