import {
  AmbientLight, AxesHelper, Box3, BoxGeometry, BufferAttribute, BufferGeometry, CanvasTexture, Color, DirectionalLight, DoubleSide, GridHelper, Group,
  Line, LineBasicMaterial, LineSegments, Matrix4, Mesh, MeshBasicMaterial, PerspectiveCamera, Points, PointsMaterial, Quaternion, Raycaster, Scene,
  SphereGeometry, Sprite, SpriteMaterial, SRGBColorSpace, Vector2, Vector3, WebGLRenderer,
} from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { TransformControls } from "three/addons/controls/TransformControls.js";
import { MTLLoader } from "three/addons/loaders/MTLLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { PLYLoader } from "three/addons/loaders/PLYLoader.js";
import type { PaintedPath, PaintedPoint, TrackingViewFrame } from "../../api/types";
import { decodeV1Tile, normalizeManifest, selectV1Tiles } from "../point-cloud/v1";
import type { CameraItem, MapTransformData, PaintDataDelta, PointCloudSource, RegistrationView, TransformMode, ViewerEngine as Contract, ViewerFilters, ViewerMetrics, ViewerMode, ViewerOptions, ViewerSelection } from "./types";

// Identity matrix perfectly matches Open3D: +X right, +Y up, +Z out of screen. (Board is vertical).
export const T_V_W = new Matrix4().identity();
export function worldToViewer(value: [number, number, number] | number[]): Vector3 {
  return new Vector3(value[0], value[1], value[2]).applyMatrix4(T_V_W);
}
function makeTextSprite(text: string, colorHex: string): Sprite {
  const canvas = document.createElement("canvas");
  canvas.width = 64;
  canvas.height = 64;
  const ctx = canvas.getContext("2d");
  if (ctx) {
    ctx.font = "Bold 44px sans-serif";
    ctx.fillStyle = colorHex;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, 32, 32);
  }
  const texture = new CanvasTexture(canvas);
  texture.needsUpdate = true;
  const material = new SpriteMaterial({ map: texture, depthTest: false });
  const sprite = new Sprite(material);
  sprite.scale.set(0.04, 0.04, 1);
  return sprite;
}

function makeLabelSprite(label: string, value: number | undefined, colorHex: string): Sprite {
  const text = value !== undefined ? `${label}: ${value}` : label;
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  if (!ctx) return new Sprite();
  ctx.font = "Bold 32px sans-serif";
  const textWidth = ctx.measureText(text).width;
  canvas.width = Math.max(128, textWidth + 32);
  canvas.height = 64;
  ctx.font = "Bold 32px sans-serif";
  ctx.fillStyle = colorHex;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, canvas.width / 2, 32);
  const texture = new CanvasTexture(canvas);
  texture.needsUpdate = true;
  const material = new SpriteMaterial({ map: texture, depthTest: false });
  const sprite = new Sprite(material);
  sprite.scale.set(canvas.width / 3000, canvas.height / 3000, 1);
  return sprite;
}

export function createLabeledAxes(size = 0.15): Group {
  const axesGroup = new Group();
  axesGroup.name = "labeled_axes";
  const axes = new AxesHelper(size);
  axesGroup.add(axes);

  const labelX = makeTextSprite("X", "#ff4d4d");
  labelX.position.set(size + 0.02, 0, 0);
  axesGroup.add(labelX);

  const labelY = makeTextSprite("Y", "#4dff4d");
  labelY.position.set(0, size + 0.02, 0);
  axesGroup.add(labelY);

  const labelZ = makeTextSprite("Z", "#4d94ff");
  labelZ.position.set(0, 0, size + 0.02);
  axesGroup.add(labelZ);

  return axesGroup;
}

function disposeGroup(group: Group): void {
  group.traverse((object) => {
    const renderable = object as Mesh | Line | Points;
    renderable.geometry?.dispose();
    const material = renderable.material;
    if (Array.isArray(material)) material.forEach((item) => item.dispose()); else material?.dispose();
  });
  group.clear();
}

export class ViewerEngine implements Contract {
  private container: HTMLElement | null = null;
  private renderer: WebGLRenderer | null = null;
  private scene: Scene | null = null;
  private camera: PerspectiveCamera | null = null;
  private controls: OrbitControls | null = null;
  private transformControls: TransformControls | null = null;
  private transformMode: TransformMode = "none";
  private prevScale = new Vector3(1, 1, 1);
  private onKeyDown: ((event: KeyboardEvent) => void) | null = null;
  private frame = 0;
  private disposed = false;
  private initialized = false;
  private contextLost = false;
  private mode: ViewerMode = "mapping";
  private budget = 3_000_000;
  private dpr = 1;
  private lastFrame = performance.now();
  private frameTime = 0;
  private loadedPoints = 0;
  private loadedTiles = 0;
  private controller: AbortController | null = null;
  private readonly map = new Group();
  private readonly objMesh = new Group();
  private readonly transformPivot = new Group();
  private readonly camerasGroup = new Group();
  private readonly registration = new Group();
  private readonly tracking = new Group();
  private readonly paint = new Group();
  private readonly helpers = new Group();
  private readonly paintObjects = new Map<string, Mesh | Line>();
  private initialMapTransform = new Matrix4();
  private filters: ViewerFilters = { showMap: true, showFrames: true, showProbe: true, showBoard: true, showPoints: true, showPaths: true, pointSize: .012, pointBudget: 3_000_000 };
  private probeGeometry: number[][] | null = null;
  private probeGroup: Group | null = null;
  private cameraIntrinsics?: { matrix: number[]; width: number; height: number; };
  private isAruco: boolean = false;
  public onCameraSelect?: (camera: CameraItem) => void;

  private getViewerTransform(): Matrix4 {
    return T_V_W.clone();
  }

  private worldToViewer(value: [number, number, number] | number[]): Vector3 {
    return new Vector3(value[0], value[1], value[2]).applyMatrix4(this.getViewerTransform());
  }
  public onCameraDoubleClick?: ((camera: CameraItem) => void) | null;

  async initialize(container: HTMLElement, options: ViewerOptions): Promise<void> {
    if (this.initialized) return;
    if (this.disposed) throw new Error("A disposed viewer cannot be initialized.");
    if (container.clientWidth < 1 || container.clientHeight < 1) throw new Error("Viewer container must have non-zero dimensions.");
    this.container = container; this.mode = options.mode; this.budget = options.pointBudget ?? this.budget;
    this.scene = new Scene(); this.scene.background = new Color(options.background ?? 0x080c11);
    this.camera = new PerspectiveCamera(48, container.clientWidth / container.clientHeight, .002, 2500); this.camera.position.set(.45, .35, .55); this.camera.up.set(0, 0, 1);
    this.renderer = new WebGLRenderer({ antialias: true, powerPreference: "high-performance" }); this.renderer.outputColorSpace = SRGBColorSpace;
    this.dpr = Math.min(window.devicePixelRatio || 1, 2); this.renderer.setPixelRatio(this.dpr); this.renderer.setSize(container.clientWidth, container.clientHeight, false); container.appendChild(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement); this.controls.enableDamping = true; this.controls.dampingFactor = .08;
    this.transformControls = new TransformControls(this.camera, this.renderer.domElement);
    this.transformControls.addEventListener("dragging-changed", (event) => { if (this.controls) this.controls.enabled = !event.value; });
    this.transformControls.addEventListener("change", () => {
      if (this.transformControls?.getMode() === "scale" && this.transformPivot) {
        let changed = this.transformPivot.scale.x;
        if (this.transformPivot.scale.y !== this.prevScale.y) changed = this.transformPivot.scale.y;
        if (this.transformPivot.scale.z !== this.prevScale.z) changed = this.transformPivot.scale.z;
        if (changed <= 0.0001) changed = 0.0001;
        this.transformPivot.scale.set(changed, changed, changed);
        this.prevScale.copy(this.transformPivot.scale);
      }
    });
    this.transformPivot.add(this.map, this.camerasGroup, this.objMesh);
    this.scene.add(this.transformPivot, this.registration, this.tracking, this.paint, this.helpers, this.transformControls.getHelper(), new AmbientLight(0xa7b9cd, 1.3), new DirectionalLight(0xffffff, 1.7));
    const grid = new GridHelper(10, 50, 0x29415a, 0x162332); grid.material.opacity = .42; grid.material.transparent = true; grid.rotation.x = Math.PI / 2;
    const worldAxes = createLabeledAxes(0.15);
    this.helpers.add(grid, worldAxes);
    this.renderer.domElement.addEventListener("webglcontextlost", this.onLost); this.renderer.domElement.addEventListener("webglcontextrestored", this.onRestored);

    // Camera pyramid click picking
    const raycaster = new Raycaster();
    const mouse = new Vector2();
    this.renderer.domElement.addEventListener("click", (event: MouseEvent) => {
      if (!this.container || !this.camera || !this.camerasGroup.children.length) return;
      const rect = this.container.getBoundingClientRect();
      mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(mouse, this.camera);
      const intersects = raycaster.intersectObjects(this.camerasGroup.children, true);
      if (intersects.length > 0) {
        const hit = intersects.find((item) => item.object.userData?.isCamera);
        if (hit) {
          const data = hit.object.userData as CameraItem & { mesh?: LineSegments };
          this.camerasGroup.traverse((obj) => {
            if (obj instanceof LineSegments && obj.material instanceof LineBasicMaterial) {
              obj.material.color.setHex(0xffea00);
            }
          });
          if (data.mesh?.material instanceof LineBasicMaterial) {
            data.mesh.material.color.setHex(0x00ffff);
          }
          this.onCameraSelect?.(data);
        }
      }
    });

    // Camera pyramid double-click — open image popup; otherwise retarget orbit control to nearby map points.
    this.renderer.domElement.addEventListener("dblclick", (event: MouseEvent) => {
      if (!this.container || !this.camera) return;
      const rect = this.container.getBoundingClientRect();
      mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(mouse, this.camera);
      const intersects = raycaster.intersectObjects(this.camerasGroup.children, true);
      if (intersects.length > 0) {
        const hit = intersects.find((item) => item.object.userData?.isCamera);
        if (hit) {
          const data = hit.object.userData as CameraItem;
          this.onCameraDoubleClick?.(data);
          return;
        }
      }

      raycaster.params.Points.threshold = Math.max(0.01, (this.filters.pointSize ?? 0.012) * 2.5);
      const pointHits = raycaster.intersectObjects(this.map.children, true).filter((item) => item.object instanceof Points);
      const nearestPoint = pointHits[0];
      if (!nearestPoint || !this.controls) return;
      if (this.transformMode !== "none") this.recenterTransformPivot(nearestPoint.point);
      this.controls.target.copy(nearestPoint.point);
      this.controls.update();
    });

    this.onKeyDown = (event: KeyboardEvent) => {
      if (document.activeElement?.tagName === "INPUT" || document.activeElement?.tagName === "TEXTAREA") return;
      const key = event.key.toLowerCase();
      if (key === "t") this.setTransformMode("translate");
      else if (key === "r") this.setTransformMode("rotate");
      else if (key === "s") this.setTransformMode("scale");
      else if (key === "escape") this.setTransformMode("none");
    };
    window.addEventListener("keydown", this.onKeyDown);
    this.initialized = true; this.visibility(); this.loop();
  }

  setMode(mode: ViewerMode): void { this.mode = mode; this.visibility(); }

  async loadMap(source: PointCloudSource): Promise<void> {
    this.ensure(); this.controller?.abort(); const controller = new AbortController(); this.controller = controller;
    disposeGroup(this.map); this.loadedPoints = 0; this.loadedTiles = 0;
    this.transformPivot.position.set(0, 0, 0);
    this.transformPivot.quaternion.set(0, 0, 0, 1);
    this.transformPivot.scale.set(1, 1, 1);
    this.map.position.set(0, 0, 0);
    this.map.quaternion.set(0, 0, 0, 1);
    this.map.scale.set(1, 1, 1);
    this.prevScale.set(1, 1, 1);
    const response = await fetch(source.manifestUrl, { signal: controller.signal, credentials: "same-origin" });
    if (!response.ok) throw new Error(`Point-cloud manifest failed (${response.status}).`);
    const rawJson = await response.json();
    const manifest = normalizeManifest(rawJson);
    if ((rawJson as any).userTransform) {
      const { position, quaternion, scale } = (rawJson as any).userTransform;
      if (position?.length === 3) this.transformPivot.position.set(position[0], position[1], position[2]);
      if (quaternion?.length === 4) this.transformPivot.quaternion.set(quaternion[0], quaternion[1], quaternion[2], quaternion[3]);
      if (typeof scale === "number" && scale > 0) {
        this.transformPivot.scale.set(scale, scale, scale);
        this.prevScale.set(scale, scale, scale);
      }
    }
    this.transformPivot.updateMatrixWorld(true);
    const mapTransform = T_V_W.clone();
    if (manifest.coordinateFrame === "M0" && manifest.similaritySWM0) {
      const { scale, rotation, translation } = manifest.similaritySWM0;
      if (!(Number.isFinite(scale) && scale > 0 && rotation.length === 9 && translation.length === 3)) throw new Error("Map similarity metadata is invalid.");
      const s = new Matrix4().set(
        scale * rotation[0], scale * rotation[1], scale * rotation[2], translation[0],
        scale * rotation[3], scale * rotation[4], scale * rotation[5], translation[1],
        scale * rotation[6], scale * rotation[7], scale * rotation[8], translation[2],
        0, 0, 0, 1,
      );
      mapTransform.multiply(s);
    }
    this.initialMapTransform.copy(mapTransform);
    this.map.matrixAutoUpdate = false;
    this.map.matrix.copy(mapTransform);
    this.map.matrix.decompose(this.map.position, this.map.quaternion, this.map.scale);
    this.map.updateMatrixWorld(true);
    const domainFromViewer = this.transformPivot.matrixWorld.clone().multiply(mapTransform).invert();
    const position = this.camera!.position.clone().applyMatrix4(domainFromViewer);
    const queue = selectV1Tiles(manifest, [position.x, position.y, position.z], this.budget);
    const work = [...queue];
    const worker = async () => {
      while (work.length && !controller.signal.aborted) {
        const tile = work.shift()!;
        const url = source.tileUrl?.(tile.id) ?? `/api/v1/projects/${source.projectId}/maps/${source.mapId}/point-cloud/tiles/${encodeURIComponent(tile.id)}`;
        const tileResponse = await fetch(url, { signal: controller.signal, credentials: "same-origin" });
        if (!tileResponse.ok) throw new Error(`Point tile ${tile.id} failed (${tileResponse.status}).`);
        const data = decodeV1Tile(await tileResponse.arrayBuffer(), tile);
        const geometry = new BufferGeometry(); geometry.setAttribute("position", new BufferAttribute(data.positions, 3)); geometry.setAttribute("color", new BufferAttribute(data.colors, 3, true)); geometry.computeBoundingSphere();
        this.map.add(new Points(geometry, new PointsMaterial({ size: this.filters.pointSize ?? .012, vertexColors: true, sizeAttenuation: true })));
        this.loadedPoints += data.pointCount; this.loadedTiles += 1;
      }
    };
    await Promise.all(Array.from({ length: Math.min(4, work.length) }, worker));
    if (!controller.signal.aborted && this.loadedPoints) this.frameObject(this.transformPivot);

    const rawCamsArray: any[] = (rawJson as any).registered_cameras ?? (rawJson as any).registeredCameras ?? [];
    if (rawCamsArray.length > 0) {
      const camList: CameraItem[] = rawCamsArray.map((c: any, i: number) => ({
        id: c.id ?? `cam-${i + 1}`,
        name: c.name ?? `Camera Frame #${i + 1}`,
        frame_id: c.frame_id ?? c.frameId,
        position: c.position as [number, number, number],
        quaternion: c.quaternion as [number, number, number, number] | undefined,
        lookAt: c.lookAt as [number, number, number] | undefined,
      }));
      this.setCameras(camList);
    } else {
      // No COLMAP cameras available for this map — clear any stale pyramids
      this.setCameras([]);
    }
  }

  private camScale = 0.19 / 0.08;

  setCameras(cameras: (CameraItem & { lookAt?: [number, number, number] })[]): void {
    disposeGroup(this.camerasGroup);
    if (this.camerasGroup.parent !== this.map) {
      this.map.add(this.camerasGroup);
    }
    if (!cameras || !cameras.length) return;
    const w = 0.06, h = 0.045, d = 0.10;
    const vertices = new Float32Array([
      0, 0, 0, w, h, d, 0, 0, 0, -w, h, d,
      0, 0, 0, -w, -h, d, 0, 0, 0, w, -h, d,
      w, h, d, -w, h, d, -w, h, d, -w, -h, d,
      -w, -h, d, w, -h, d, w, -h, d, w, h, d,
      0, -h, d, 0, -h - 0.03, d
    ]);
    const camGeom = new BufferGeometry();
    camGeom.setAttribute("position", new BufferAttribute(vertices, 3));
    const pickGeom = new BoxGeometry(0.16, 0.16, 0.18);
    const pickMat = new MeshBasicMaterial({ visible: false });

    cameras.forEach((cam, idx) => {
      const camMat = new LineBasicMaterial({ color: 0xffea00 });
      const camMesh = new LineSegments(camGeom, camMat);
      const pickMesh = new Mesh(pickGeom, pickMat);
      pickMesh.userData = { isCamera: true, cameraIndex: idx, name: cam.name ?? `Camera #${idx + 1}`, mesh: camMesh, ...cam };

      const group = new Group();
      group.add(camMesh);
      group.add(pickMesh);

      group.position.set(cam.position[0], cam.position[1], cam.position[2]);
      if (cam.quaternion && cam.quaternion.length === 4) {
        group.quaternion.set(cam.quaternion[0], cam.quaternion[1], cam.quaternion[2], cam.quaternion[3]);
      } else if (cam.lookAt) {
        group.lookAt(new Vector3(...cam.lookAt));
      }
      group.scale.set(this.camScale, this.camScale, this.camScale);
      this.camerasGroup.add(group);
    });
    this.visibility();
  }

  setCamSize(size: number): void {
    const clean = Math.max(0.01, size);
    this.camScale = clean / 0.08;
    this.camerasGroup.children.forEach((group) => {
      group.scale.set(this.camScale, this.camScale, this.camScale);
    });
  }

  setTransformMode(mode: TransformMode): void {
    this.transformMode = mode;
    if (!this.transformControls) return;
    if (mode === "none") {
      this.transformControls.detach();
    } else {
      this.transformControls.setMode(mode);
      this.transformControls.attach(this.transformPivot);
    }
  }

  getMapTransform(): MapTransformData {
    this.map.updateMatrixWorld(true);
    const matrix = this.map.matrixWorld.clone().multiply(this.initialMapTransform.clone().invert());
    const position = new Vector3();
    const quaternion = new Quaternion();
    const scale = new Vector3();
    matrix.decompose(position, quaternion, scale);
    return {
      position: [position.x, position.y, position.z],
      quaternion: [quaternion.x, quaternion.y, quaternion.z, quaternion.w],
      scale: scale.x,
    };
  }

  resetMapTransform(): void {
    this.transformPivot.position.set(0, 0, 0);
    this.transformPivot.quaternion.set(0, 0, 0, 1);
    this.transformPivot.scale.set(1, 1, 1);
    this.map.matrix.copy(this.initialMapTransform);
    this.map.matrix.decompose(this.map.position, this.map.quaternion, this.map.scale);
    this.transformPivot.updateMatrixWorld(true);
    this.map.updateMatrixWorld(true);
    this.prevScale.set(1, 1, 1);
  }

  setPointSize(size: number): void {
    const clean = Math.max(0.001, size);
    this.filters.pointSize = clean;
    this.map.traverse((object) => {
      if (object instanceof Points) object.material.size = clean;
    });
  }

  setRegistration(value: RegistrationView): void {
    if (!this.initialized || this.disposed) return; disposeGroup(this.registration);

    this.isAruco = Boolean((value as any).is_aruco_mode || value.board_definition?.layout || value.board_definition);
    const tvw = this.getViewerTransform();

    if (value.board_definition?.layout) {
      const layout = value.board_definition.layout as Record<string, number[][]>;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const corners of Object.values(layout)) {
        for (const pt of corners) {
          if (pt[0] < minX) minX = pt[0];
          if (pt[1] < minY) minY = pt[1];
          if (pt[0] > maxX) maxX = pt[0];
          if (pt[1] > maxY) maxY = pt[1];
        }
      }
      if (minX !== Infinity) {
        const boardGroup = new Group();
        const margin = 0.03;
        const width = Math.max(0.04, maxX - minX + 2 * margin);
        const height = Math.max(0.04, maxY - minY + 2 * margin);
        const centerX = (minX + maxX) / 2;
        const centerY = (minY + maxY) / 2;

        // 1. Base board plate (Bright White/Slate surface, visible from both sides)
        const planeGeo = new BoxGeometry(width, height, 0.003);
        const planeMat = new MeshBasicMaterial({ color: 0xe2e8f0, side: DoubleSide });
        const plane = new Mesh(planeGeo, planeMat);
        plane.position.set(centerX, centerY, -0.0015);
        boardGroup.add(plane);

        // 2. Outer board cyan border outline
        const borderPts = [
          new Vector3(centerX - width / 2, centerY - height / 2, 0.0002),
          new Vector3(centerX + width / 2, centerY - height / 2, 0.0002),
          new Vector3(centerX + width / 2, centerY + height / 2, 0.0002),
          new Vector3(centerX - width / 2, centerY + height / 2, 0.0002),
          new Vector3(centerX - width / 2, centerY - height / 2, 0.0002),
        ];
        const borderGeom = new BufferGeometry().setFromPoints(borderPts);
        boardGroup.add(new Line(borderGeom, new LineBasicMaterial({ color: 0x00f0ff, linewidth: 3 })));

        // 3. ArUco markers (filled dark square + thick vibrant outline + yellow orientation dot)
        const palette = [0xff3b30, 0x34c759, 0x007aff, 0xaf52de, 0xff9500];
        let markerIndex = 0;
        for (const corners of Object.values(layout)) {
          if (corners.length < 4) continue;
          const color = palette[markerIndex % palette.length];
          const P0 = new Vector3(corners[0][0], corners[0][1], 0.001);
          const P1 = new Vector3(corners[1][0], corners[1][1], 0.001);
          const P2 = new Vector3(corners[2][0], corners[2][1], 0.001);
          const P3 = new Vector3(corners[3][0], corners[3][1], 0.001);

          // Marker filled dark square
          const shapeGeom = new BufferGeometry();
          const verts = new Float32Array([
            P0.x, P0.y, P0.z, P1.x, P1.y, P1.z, P2.x, P2.y, P2.z,
            P0.x, P0.y, P0.z, P2.x, P2.y, P2.z, P3.x, P3.y, P3.z,
          ]);
          shapeGeom.setAttribute("position", new BufferAttribute(verts, 3));
          const markerMesh = new Mesh(shapeGeom, new MeshBasicMaterial({ color: 0x1e293b, side: DoubleSide }));
          boardGroup.add(markerMesh);

          // Marker colored outline
          const lineGeom = new BufferGeometry().setFromPoints([P0, P1, P2, P3, P0]);
          boardGroup.add(new Line(lineGeom, new LineBasicMaterial({ color, linewidth: 3 })));

          // Corner 0 yellow orientation dot
          const dot = this.sphere(0.004, 0xffd60a);
          dot.position.copy(P0);
          boardGroup.add(dot);

          markerIndex++;
        }

        if (value.t_w_b?.length === 16) boardGroup.applyMatrix4(tvw.clone().multiply(new Matrix4().fromArray(value.t_w_b).transpose()));
        else boardGroup.applyMatrix4(tvw);
        this.registration.add(boardGroup);
      }
    } else {
      const board = createLabeledAxes(.12);
      if (value.t_w_b?.length === 16) board.applyMatrix4(tvw.clone().multiply(new Matrix4().fromArray(value.t_w_b).transpose()));
      else board.applyMatrix4(tvw);
      this.registration.add(board);
    }

    value.observations?.forEach((point) => { const marker = this.sphere(.004, 0x58d6ff); marker.position.copy(this.worldToViewer(point)); this.registration.add(marker); });
    value.residualPoints?.forEach((item) => this.registration.add(new Line(new BufferGeometry().setFromPoints([this.worldToViewer(item.from), this.worldToViewer(item.to)]), new LineBasicMaterial({ color: item.errorMm > 3 ? 0xff7479 : item.errorMm > 1.5 ? 0xf2bd55 : 0x61e2b1 }))));
    this.visibility();
  }

  setProbeGeometry(points: number[][]): void {
    this.probeGeometry = points;
    this.rebuildProbeGroup();
  }

  setCameraIntrinsics(intrinsics: { matrix: number[]; width: number; height: number; }): void {
    this.cameraIntrinsics = intrinsics;
    const existing = this.tracking.getObjectByName("live_cam");
    if (existing) {
      existing.removeFromParent();
      if (existing instanceof LineSegments) existing.geometry.dispose();
    }
  }

  rebuildProbeGroup(): void {
    if (this.probeGroup) disposeGroup(this.probeGroup);
    if (!this.probeGeometry) return;
    this.probeGroup = new Group();

    const pts = this.probeGeometry.map(p => new Vector3(p[0], p[1], p[2]));
    const group = this.probeGroup;
    const geom = new BufferGeometry().setFromPoints(pts);
    group.add(new Line(geom, new LineBasicMaterial({ color: 0xffea00, linewidth: 2 })));

    pts.forEach(p => {
      const s = this.sphere(.004, 0x61e2b1);
      s.position.copy(p);
      group.add(s);
    });
  }

  applyTrackingFrame(value: TrackingViewFrame): void {
    if (!this.initialized || this.disposed) return;
    const t_w_c = (value as any).t_w_c as number[] | undefined;
    const t_c_m = (value as any).t_c_m as number[] | undefined;

    if (this.probeGroup && value.probe_state === "tracked" && t_w_c && t_c_m) {
      if (this.probeGroup.parent !== this.tracking) this.tracking.add(this.probeGroup);
      this.probeGroup.visible = true;

      const matC = new Matrix4().fromArray(t_w_c).transpose();
      const matM = new Matrix4().fromArray(t_c_m).transpose();
      const matW = matC.multiply(matM);
      const viewerMat = this.getViewerTransform().multiply(matW);
      viewerMat.decompose(this.probeGroup.position, this.probeGroup.quaternion, this.probeGroup.scale);

      const tip = this.tracking.getObjectByName("tip") as Mesh | undefined;
      if (tip) tip.visible = false;
    } else {
      if (this.probeGroup) this.probeGroup.visible = false;
      let tip = this.tracking.getObjectByName("tip") as Mesh | undefined;
      if (!tip) { tip = this.sphere(.006, 0x61e2b1); tip.name = "tip"; this.tracking.add(tip); }
      tip.visible = value.probe_state === "tracked" && Boolean(value.tip_w_m);
      if (value.tip_w_m) { tip.position.copy(this.worldToViewer(value.tip_w_m)); (tip.material as MeshBasicMaterial).color.set(value.quality === "good" ? 0x61e2b1 : value.quality === "warning" ? 0xf2bd55 : 0xff7479); }
    }

    let camMesh = this.tracking.getObjectByName("live_cam") as LineSegments | undefined;
    if (!camMesh) {
      let w = 0.045, h = 0.08, d = 0.12;
      if (this.cameraIntrinsics) {
        const { matrix, width, height } = this.cameraIntrinsics;
        const fx = matrix[0];
        const fy = matrix[4];
        w = d * width / (2 * fx);
        h = d * height / (2 * fy);
      }
      const vertices = new Float32Array([
        0, 0, 0, w, h, d, 0, 0, 0, -w, h, d,
        0, 0, 0, -w, -h, d, 0, 0, 0, w, -h, d,
        w, h, d, -w, h, d, -w, h, d, -w, -h, d,
        -w, -h, d, w, -h, d, w, -h, d, w, h, d,
        0, -h, d, 0, -h - 0.03, d
      ]);
      const geom = new BufferGeometry();
      geom.setAttribute("position", new BufferAttribute(vertices, 3));
      camMesh = new LineSegments(geom, new LineBasicMaterial({ color: 0x00ffcc, linewidth: 3 }));
      camMesh.name = "live_cam";
      this.tracking.add(camMesh);
    }

    const isTracked = value.camera_state === "tracked" || Boolean(t_w_c);
    if (t_w_c && t_w_c.length === 16 && isTracked) {
      camMesh.visible = true;
      const mat = new Matrix4().fromArray(t_w_c).transpose();
      const viewerMat = this.getViewerTransform().multiply(mat);
      viewerMat.decompose(camMesh.position, camMesh.quaternion, camMesh.scale);
      camMesh.scale.multiplyScalar(this.camScale);
      console.log(`📷 [3D Viewer] Live Camera Position Updated: X=${camMesh.position.x.toFixed(3)} Y=${camMesh.position.y.toFixed(3)} Z=${camMesh.position.z.toFixed(3)}`);
    } else {
      camMesh.visible = false;
    }
  }

  setPaintData(delta: PaintDataDelta): void {
    if (!this.initialized || this.disposed) return;
    if (delta.reset) { disposeGroup(this.paint); this.paintObjects.clear(); }
    delta.removeIds?.forEach((id) => this.removePaint(id));
    delta.upsert?.forEach((record) => this.upsertPaint(record));
    delta.provisional?.forEach((record) => { this.removePaint(record.id); const marker = this.sphere(.0045, 0x58d6ff, .6); marker.position.copy(this.worldToViewer(record.position)); marker.userData = { recordType: "point", quality: record.quality }; this.paint.add(marker); this.paintObjects.set(record.id, marker); });
    this.visibility();
  }

  setFilters(value: ViewerFilters): void {
    this.filters = { ...this.filters, ...value }; if (value.pointBudget) this.budget = value.pointBudget;
    if (value.pointSize) this.map.traverse((object) => { if (object instanceof Points) object.material.size = value.pointSize!; }); this.visibility();
  }
  setSelection(value: ViewerSelection): void {
    this.paintObjects.forEach((object, id) => object.scale.setScalar(id === value.id ? 1.8 : 1));
    if (value.position && this.controls) { this.controls.target.copy(this.worldToViewer(value.position)); this.controls.update(); }
  }
  resize(width: number, height: number, dpr: number): void { if (!this.camera || !this.renderer || width < 1 || height < 1) return; this.camera.aspect = width / height; this.camera.updateProjectionMatrix(); this.dpr = Math.min(Math.max(.75, dpr), 2); this.renderer.setPixelRatio(this.dpr); this.renderer.setSize(width, height, false); }
  getMetrics(): ViewerMetrics { return { visiblePoints: this.map.visible ? this.loadedPoints : 0, loadedPoints: this.loadedPoints, loadedTiles: this.loadedTiles, drawCalls: this.renderer?.info.render.calls ?? 0, frameTimeMs: this.frameTime, pixelRatio: this.dpr, contextLost: this.contextLost }; }
  resetView(): void { if (!this.camera || !this.controls) return; this.camera.position.set(.45, .35, .55); this.controls.target.set(0, 0, 0); this.controls.update(); }
  async loadMesh(projectId: string, mapId: string): Promise<void> {
    const basePath = `/api/v1/projects/${projectId}/maps/${mapId}/openmvs/`;
    
    // Clean up existing mesh
    disposeGroup(this.objMesh);
    if (!this.objMesh.parent && this.transformPivot) {
      this.transformPivot.add(this.objMesh);
    }
    
    try {
      // 1. Try loading 100% full-color PLY mesh first
      const plyLoader = new PLYLoader();
      try {
        const geometry = await plyLoader.loadAsync(basePath + "colored_mesh.ply");
        geometry.computeVertexNormals();
        const material = new MeshBasicMaterial({
          vertexColors: true,
          side: DoubleSide,
        });
        const mesh = new Mesh(geometry, material);
        this.objMesh.add(mesh);
        this.objMesh.visible = true;
        this.transformPivot.updateMatrixWorld(true);
        this.frameObject(this.objMesh);
        return;
      } catch {
        // colored_mesh.ply not found, fall back to OBJ
      }

      // 2. Fallback to OBJ
      const { TextureLoader } = await import("three");
      const atlasTexture = await new TextureLoader().loadAsync(basePath + "model_dense_texture_material_00_map_Kd.jpg");
      atlasTexture.flipY = false;
      atlasTexture.colorSpace = SRGBColorSpace;

      const objLoader = new OBJLoader();
      objLoader.setPath(basePath);
      const object = await objLoader.loadAsync("model_dense_texture.obj");

      object.traverse((child) => {
        if ((child as Mesh).isMesh) {
          const basicMat = new MeshBasicMaterial({
            map: atlasTexture,
            side: DoubleSide,
          });
          (child as Mesh).material = basicMat;
        }
      });
      
      this.objMesh.add(object);
      this.objMesh.visible = true;
      this.transformPivot.updateMatrixWorld(true);
      this.frameObject(this.objMesh);
    } catch (err) {
      console.error("Failed to load mesh:", err);
    }
  }

  setMeshVisibility(visible: boolean): void {
    if (!this.objMesh.parent && this.transformPivot) {
      this.transformPivot.add(this.objMesh);
    }
    this.objMesh.visible = visible;
    if (visible) {
      this.transformPivot.updateMatrixWorld(true);
      if (this.objMesh.children.length > 0) {
        this.frameObject(this.objMesh);
      }
    }
  }

  dispose(): void {
    if (this.disposed) return; this.disposed = true; this.controller?.abort(); cancelAnimationFrame(this.frame); this.controls?.dispose(); this.transformControls?.dispose();
    if (this.onKeyDown) window.removeEventListener("keydown", this.onKeyDown);
    if (this.renderer) { this.renderer.domElement.removeEventListener("webglcontextlost", this.onLost); this.renderer.domElement.removeEventListener("webglcontextrestored", this.onRestored); this.renderer.dispose(); this.renderer.forceContextLoss(); this.renderer.domElement.remove(); }
    [this.map, this.registration, this.tracking, this.paint, this.helpers].forEach(disposeGroup); this.paintObjects.clear(); this.scene?.clear(); this.container = null; this.renderer = null; this.scene = null; this.camera = null; this.controls = null; this.transformControls = null;
  }

  private loop = () => { if (this.disposed || !this.renderer || !this.scene || !this.camera) return; const now = performance.now(); this.frameTime = this.frameTime * .9 + (now - this.lastFrame) * .1; this.lastFrame = now; this.controls?.update(); if (!this.contextLost) this.renderer.render(this.scene, this.camera); this.frame = requestAnimationFrame(this.loop); };
  private onLost = (event: Event) => { event.preventDefault(); this.contextLost = true; };
  private onRestored = () => { this.contextLost = false; this.renderer?.info.reset(); };
  private visibility() {
    this.map.visible = this.filters.showMap !== false;
    this.map.traverse((object) => {
      if (object instanceof Points) {
        object.visible = this.filters.showPoints !== false;
      }
    });
    this.camerasGroup.visible = this.filters.showFrames !== false;
    this.helpers.visible = this.filters.showFrames !== false;
    this.registration.visible = this.filters.showBoard !== false;
    this.tracking.visible = (this.mode === "live" || this.mode === "registration") && this.filters.showProbe !== false;
    this.paint.visible = this.mode === "live" || this.mode === "review";
    this.paintObjects.forEach((object) => {
      const data = object.userData;
      object.visible = (data.recordType !== "point" || this.filters.showPoints !== false) && (data.recordType !== "path" || this.filters.showPaths !== false) && (!data.deleted || this.filters.includeDeleted === true) && (!this.filters.quality || this.filters.quality === "all" || data.quality === this.filters.quality);
    });
  }
  private upsertPaint(record: PaintedPoint | PaintedPath) {
    this.removePaint(record.id);
    if (record.type === "point" && !record.position_w_m) return;
    let object: Mesh | Line;
    if (record.type === "point") {
      const color = record.color ? parseInt(record.color.replace("#", "0x")) : (record.deleted ? 0x667487 : record.quality === "good" ? 0x61e2b1 : record.quality === "warning" ? 0xf2bd55 : 0xff7479);
      object = this.sphere(.0045, color, record.deleted ? .35 : .95);
      object.position.copy(this.worldToViewer(record.position_w_m!));
      if (record.label || record.value !== undefined) {
        const sprite = makeLabelSprite(record.label || "Point", record.value, record.color || "#ffffff");
        sprite.position.set(0, 0.015, 0);
        object.add(sprite);
      }
    } else {
      object = new Line(new BufferGeometry().setFromPoints(record.positions_w_m.map(p => this.worldToViewer(p))), new LineBasicMaterial({ color: record.deleted ? 0x667487 : 0x58d6ff, transparent: true, opacity: record.deleted ? .3 : .9 }));
    }
    object.userData = { recordType: record.type, quality: record.quality, deleted: record.deleted };
    this.paint.add(object);
    this.paintObjects.set(record.id, object);
  }
  private removePaint(id: string) { const object = this.paintObjects.get(id); if (!object) return; object.removeFromParent(); object.geometry.dispose(); const material = object.material; if (Array.isArray(material)) material.forEach((item) => item.dispose()); else material.dispose(); this.paintObjects.delete(id); }
  private sphere(radius: number, color: number, opacity = 1): Mesh { return new Mesh(new SphereGeometry(radius, 12, 8), new MeshBasicMaterial({ color, transparent: opacity < 1, opacity })); }
  private recenterTransformPivot(point: Vector3): void {
    this.map.updateMatrixWorld(true);
    const worldMatrix = this.map.matrixWorld.clone();
    this.transformPivot.position.copy(point);
    this.transformPivot.quaternion.set(0, 0, 0, 1);
    this.transformPivot.scale.set(1, 1, 1);
    this.transformPivot.updateMatrixWorld(true);
    const localMatrix = this.transformPivot.matrixWorld.clone().invert().multiply(worldMatrix);
    localMatrix.decompose(this.map.position, this.map.quaternion, this.map.scale);
    this.map.updateMatrixWorld(true);
  }
  private frameObject(group: Group) { if (!this.camera || !this.controls) return; const box = new Box3().setFromObject(group); if (box.isEmpty()) return; const size = box.getSize(new Vector3()); const center = box.getCenter(new Vector3()); const distance = Math.max(size.x, size.y, size.z, .1) * 1.8; this.controls.target.copy(center); this.camera.position.copy(center).add(new Vector3(distance, distance * .7, distance)); this.camera.near = Math.max(distance / 10000, .001); this.camera.far = Math.max(distance * 20, 20); this.camera.updateProjectionMatrix(); this.controls.update(); }
  private ensure() { if (!this.initialized || this.disposed) throw new Error("ViewerEngine is not available."); }
}
