export interface V1Bounds { min: [number, number, number]; max: [number, number, number] }
export interface V1Tile {
  id: string;
  uri: string;
  bounds: V1Bounds;
  pointCount: number;
  geometricErrorM: number;
  children: string[];
  sha256?: string;
}
export interface MarkerItem {
  id: number;
  marker_id?: number;
  corners: [number, number, number][];
  center: [number, number, number];
  normal?: [number, number, number];
  observation_count?: number;
}

export interface V1Manifest {
  format: "spatial-probe-atlas-octree";
  version: 1;
  coordinateFrame: "W" | "M0";
  units: "m" | "arbitrary";
  similaritySWM0?: { scale: number; rotation: number[]; translation: number[] };
  bounds: V1Bounds;
  pointCount: number;
  rootTiles: string[];
  tiles: Record<string, V1Tile>;
  registered_cameras?: any[];
  registeredCameras?: any[];
  registered_markers?: MarkerItem[];
  registeredMarkers?: MarkerItem[];
}

type RawManifest = {
  format: string;
  version: number;
  coordinate_frame?: string;
  similarity_s_w_m0?: { scale: number; rotation: number[]; translation: number[] };
  units?: string;
  point_count?: number;
  bounds: { min: number[]; max: number[] } | number[];
  root_tiles?: string[];
  tiles: Record<string, {
    tile_id?: string;
    uri?: string;
    bounds: { min: number[]; max: number[] } | number[];
    point_count: number;
    children?: string[];
    geometric_error_m?: number;
    geometric_error?: number;
    sha256?: string;
  }>;
};

function bounds(value: RawManifest["bounds"]): V1Bounds {
  if (Array.isArray(value)) return { min: [value[0], value[1], value[2]], max: [value[3], value[4], value[5]] };
  return { min: [value.min[0], value.min[1], value.min[2]], max: [value.max[0], value.max[1], value.max[2]] };
}

export function normalizeManifest(value: unknown): V1Manifest {
  const raw = value as RawManifest;
  if (!raw || raw.format !== "spatial-probe-atlas-octree" || raw.version !== 1 || !raw.tiles) throw new Error("Unsupported point-cloud manifest.");
  const tiles = Object.fromEntries(Object.entries(raw.tiles).map(([key, tile]) => [key, {
    id: tile.tile_id ?? key,
    uri: tile.uri ?? "",
    bounds: bounds(tile.bounds),
    pointCount: tile.point_count,
    geometricErrorM: tile.geometric_error_m ?? tile.geometric_error ?? 0,
    children: tile.children ?? [],
    sha256: tile.sha256,
  }]));
  return {
    format: "spatial-probe-atlas-octree",
    version: 1,
    coordinateFrame: raw.coordinate_frame === "M0" ? "M0" : "W",
    units: raw.units === "arbitrary" ? "arbitrary" : "m",
    similaritySWM0: raw.similarity_s_w_m0,
    bounds: bounds(raw.bounds),
    pointCount: raw.point_count ?? Object.values(tiles).reduce((sum, tile) => sum + tile.pointCount, 0),
    rootTiles: raw.root_tiles ?? Object.keys(tiles).filter((key) => !Object.values(tiles).some((tile) => tile.children.includes(key))),
    tiles,
  };
}

export function selectV1Tiles(manifest: V1Manifest, camera: [number, number, number], budget: number): V1Tile[] {
  const candidates = Object.values(manifest.tiles).map((tile) => {
    const center = tile.bounds.min.map((value, index) => (value + tile.bounds.max[index]) / 2);
    const distance = Math.max(0.001, Math.hypot(center[0] - camera[0], center[1] - camera[1], center[2] - camera[2]));
    return { tile, score: tile.geometricErrorM / distance };
  }).sort((left, right) => right.score - left.score || left.tile.id.localeCompare(right.tile.id));
  const selected: V1Tile[] = [];
  let points = 0;
  for (const candidate of candidates) {
    if (selected.length && points + candidate.tile.pointCount > budget) continue;
    selected.push(candidate.tile); points += candidate.tile.pointCount;
    if (points >= budget) break;
  }
  return selected;
}

export function decodeV1Tile(buffer: ArrayBuffer, descriptor: V1Tile): { positions: Float32Array; colors: Uint8Array; pointCount: number } {
  if (buffer.byteLength < 40) throw new Error(`Tile ${descriptor.id} header is truncated.`);
  const magic = new TextDecoder().decode(new Uint8Array(buffer, 0, 8));
  if (magic !== "SPATILE1") throw new Error(`Tile ${descriptor.id} has invalid magic.`);
  const view = new DataView(buffer);
  const version = view.getUint16(8, true);
  const flags = view.getUint16(10, true);
  const pointCount = view.getUint32(12, true);
  if (version !== 1) throw new Error(`Tile version ${version} is not supported.`);
  const expected = 40 + pointCount * 9;
  if (buffer.byteLength < expected) throw new Error(`Tile ${descriptor.id} payload is truncated.`);
  const headerBounds: V1Bounds = {
    min: [view.getFloat32(16, true), view.getFloat32(20, true), view.getFloat32(24, true)],
    max: [view.getFloat32(28, true), view.getFloat32(32, true), view.getFloat32(36, true)],
  };
  const positions = new Float32Array(pointCount * 3);
  const colors = new Uint8Array(pointCount * 3);
  for (let index = 0; index < pointCount; index += 1) {
    const offset = 40 + index * 9;
    for (let axis = 0; axis < 3; axis += 1) {
      const quantized = view.getUint16(offset + axis * 2, true);
      positions[index * 3 + axis] = headerBounds.min[axis] + (quantized / 65535) * (headerBounds.max[axis] - headerBounds.min[axis]);
    }
    if (flags & 1) {
      colors[index * 3] = view.getUint8(offset + 6);
      colors[index * 3 + 1] = view.getUint8(offset + 7);
      colors[index * 3 + 2] = view.getUint8(offset + 8);
    } else colors.fill(190, index * 3, index * 3 + 3);
  }
  return { positions, colors, pointCount };
}
