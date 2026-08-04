import type { PointCloudManifest, PointCloudTileDescriptor } from "../../api/types";

export interface LodCandidate {
  tile: PointCloudTileDescriptor;
  score: number;
}

export function tileDistanceSquared(tile: PointCloudTileDescriptor, camera: [number, number, number]): number {
  const [minX, minY, minZ, maxX, maxY, maxZ] = tile.bounds;
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  const centerZ = (minZ + maxZ) / 2;
  return (centerX - camera[0]) ** 2 + (centerY - camera[1]) ** 2 + (centerZ - camera[2]) ** 2;
}

export function selectTiles(
  manifest: PointCloudManifest,
  camera: [number, number, number],
  pointBudget: number,
): PointCloudTileDescriptor[] {
  const candidates: LodCandidate[] = Object.values(manifest.tiles).map((tile) => ({
    tile,
    score: tile.geometric_error / Math.max(0.000001, Math.sqrt(tileDistanceSquared(tile, camera))),
  }));
  candidates.sort((left, right) => right.score - left.score || left.tile.id.localeCompare(right.tile.id));
  const selected: PointCloudTileDescriptor[] = [];
  let used = 0;
  for (const candidate of candidates) {
    if (selected.length > 0 && used + candidate.tile.point_count > pointBudget) continue;
    selected.push(candidate.tile);
    used += candidate.tile.point_count;
    if (used >= pointBudget) break;
  }
  return selected;
}

export interface DecodedTile {
  positions: Float32Array;
  colors: Uint8Array;
  pointCount: number;
}

export function decodePointTile(buffer: ArrayBuffer, tile: PointCloudTileDescriptor, encoding = "quantized_uint16_xyz"): DecodedTile {
  if (buffer.byteLength >= 12) {
    const magic = new TextDecoder().decode(new Uint8Array(buffer, 0, 4));
    if (magic === "SPAT") return decodeSpaTile(buffer, tile);
  }
  if (encoding === "float32_xyz") return decodeFloatTile(buffer, tile);
  return decodeQuantizedTile(buffer, tile);
}

function decodeSpaTile(buffer: ArrayBuffer, tile: PointCloudTileDescriptor): DecodedTile {
  const view = new DataView(buffer);
  const version = view.getUint16(4, true);
  if (version !== 1) throw new Error(`Unsupported tile version ${version}.`);
  const flags = view.getUint16(6, true);
  const pointCount = view.getUint32(8, true);
  const headerLength = 12;
  if (flags & 1) return decodeQuantizedTile(buffer.slice(headerLength), { ...tile, point_count: pointCount }, true);
  return decodeFloatTile(buffer.slice(headerLength), { ...tile, point_count: pointCount });
}

function decodeFloatTile(buffer: ArrayBuffer, tile: PointCloudTileDescriptor): DecodedTile {
  const pointCount = tile.point_count;
  const positionsBytes = pointCount * 3 * 4;
  if (buffer.byteLength < positionsBytes) throw new Error("Point tile position payload is truncated.");
  const positions = new Float32Array(buffer.slice(0, positionsBytes));
  const colors = buffer.byteLength >= positionsBytes + pointCount * 3
    ? new Uint8Array(buffer.slice(positionsBytes, positionsBytes + pointCount * 3))
    : new Uint8Array(pointCount * 3).fill(190);
  return { positions, colors, pointCount };
}

function decodeQuantizedTile(buffer: ArrayBuffer, tile: PointCloudTileDescriptor, rgbPlanar = false): DecodedTile {
  const pointCount = tile.point_count;
  const required = pointCount * 9;
  if (buffer.byteLength < required) throw new Error("Quantized point tile payload is truncated.");
  const positions = new Float32Array(pointCount * 3);
  const colors = new Uint8Array(pointCount * 3);
  const [minX, minY, minZ, maxX, maxY, maxZ] = tile.bounds;
  const view = new DataView(buffer);
  const colorOffset = pointCount * 6;
  for (let index = 0; index < pointCount; index += 1) {
    const base = rgbPlanar ? index * 6 : index * 9;
    positions[index * 3] = minX + (view.getUint16(base, true) / 65535) * (maxX - minX);
    positions[index * 3 + 1] = minY + (view.getUint16(base + 2, true) / 65535) * (maxY - minY);
    positions[index * 3 + 2] = minZ + (view.getUint16(base + 4, true) / 65535) * (maxZ - minZ);
    const rgbBase = rgbPlanar ? colorOffset + index * 3 : base + 6;
    colors[index * 3] = view.getUint8(rgbBase);
    colors[index * 3 + 1] = view.getUint8(rgbBase + 1);
    colors[index * 3 + 2] = view.getUint8(rgbBase + 2);
  }
  return { positions, colors, pointCount };
}
