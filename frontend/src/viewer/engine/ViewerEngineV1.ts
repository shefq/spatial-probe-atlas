import {
  AmbientLight, AxesHelper, Box3, BufferAttribute, BufferGeometry, Color, DirectionalLight, GridHelper, Group,
  Line, LineBasicMaterial, Matrix4, Mesh, MeshBasicMaterial, PerspectiveCamera, Points, PointsMaterial, Scene,
  SphereGeometry, SRGBColorSpace, Vector3, WebGLRenderer,
} from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import type { PaintedPath, PaintedPoint, TrackingViewFrame } from "../../api/types";
import { decodeV1Tile, normalizeManifest, selectV1Tiles } from "../point-cloud/v1";
import type { PaintDataDelta, PointCloudSource, RegistrationView, ViewerEngine as Contract, ViewerFilters, ViewerMetrics, ViewerMode, ViewerOptions, ViewerSelection } from "./types";

export const T_V_W = new Matrix4().set(1,0,0,0, 0,0,-1,0, 0,1,0,0, 0,0,0,1);
export function worldToViewer(value: [number, number, number]): Vector3 { return new Vector3(...value).applyMatrix4(T_V_W); }

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
  private readonly registration = new Group();
  private readonly tracking = new Group();
  private readonly paint = new Group();
  private readonly helpers = new Group();
  private readonly paintObjects = new Map<string, Mesh | Line>();
  private filters: ViewerFilters = { showMap: true, showFrames: true, showProbe: true, showBoard: true, showPoints: true, showPaths: true, pointSize: .012, pointBudget: 3_000_000 };

  async initialize(container: HTMLElement, options: ViewerOptions): Promise<void> {
    if (this.initialized) return;
    if (this.disposed) throw new Error("A disposed viewer cannot be initialized.");
    if (container.clientWidth < 1 || container.clientHeight < 1) throw new Error("Viewer container must have non-zero dimensions.");
    this.container = container; this.mode = options.mode; this.budget = options.pointBudget ?? this.budget;
    this.scene = new Scene(); this.scene.background = new Color(options.background ?? 0x080c11);
    this.camera = new PerspectiveCamera(48, container.clientWidth / container.clientHeight, .002, 2500); this.camera.position.set(.45, .35, .55);
    this.renderer = new WebGLRenderer({ antialias: true, powerPreference: "high-performance" }); this.renderer.outputColorSpace = SRGBColorSpace;
    this.dpr = Math.min(window.devicePixelRatio || 1, 2); this.renderer.setPixelRatio(this.dpr); this.renderer.setSize(container.clientWidth, container.clientHeight, false); container.appendChild(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement); this.controls.enableDamping = true; this.controls.dampingFactor = .08;
    this.scene.add(this.map, this.registration, this.tracking, this.paint, this.helpers, new AmbientLight(0xa7b9cd, 1.3), new DirectionalLight(0xffffff, 1.7));
    const grid = new GridHelper(1, 20, 0x29415a, 0x162332); grid.material.opacity = .42; grid.material.transparent = true; this.helpers.add(grid, new AxesHelper(.08));
    this.renderer.domElement.addEventListener("webglcontextlost", this.onLost); this.renderer.domElement.addEventListener("webglcontextrestored", this.onRestored);
    this.initialized = true; this.visibility(); this.loop();
  }

  setMode(mode: ViewerMode): void { this.mode = mode; this.visibility(); }

  async loadMap(source: PointCloudSource): Promise<void> {
    this.ensure(); this.controller?.abort(); const controller = new AbortController(); this.controller = controller;
    disposeGroup(this.map); this.loadedPoints = 0; this.loadedTiles = 0;
    const response = await fetch(source.manifestUrl, { signal: controller.signal, credentials: "same-origin" });
    if (!response.ok) throw new Error(`Point-cloud manifest failed (${response.status}).`);
    const manifest = normalizeManifest(await response.json());
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
    this.map.matrixAutoUpdate = false;
    this.map.matrix.copy(mapTransform);
    const domainFromViewer = mapTransform.clone().invert();
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
    if (!controller.signal.aborted && this.loadedPoints) this.frameObject(this.map);
  }

  setRegistration(value: RegistrationView): void {
    if (!this.initialized || this.disposed) return; disposeGroup(this.registration);
    const board = new AxesHelper(.07);
    if (value.t_w_b?.length === 16) board.applyMatrix4(T_V_W.clone().multiply(new Matrix4().fromArray(value.t_w_b).transpose()));
    this.registration.add(board);
    value.observations?.forEach((point) => { const marker = this.sphere(.004, 0x58d6ff); marker.position.copy(worldToViewer(point)); this.registration.add(marker); });
    value.residualPoints?.forEach((item) => this.registration.add(new Line(new BufferGeometry().setFromPoints([worldToViewer(item.from), worldToViewer(item.to)]), new LineBasicMaterial({ color: item.errorMm > 3 ? 0xff7479 : item.errorMm > 1.5 ? 0xf2bd55 : 0x61e2b1 }))));
    this.visibility();
  }

  applyTrackingFrame(value: TrackingViewFrame): void {
    if (!this.initialized || this.disposed) return;
    let tip = this.tracking.getObjectByName("tip") as Mesh | undefined;
    if (!tip) { tip = this.sphere(.006, 0x61e2b1); tip.name = "tip"; this.tracking.add(tip); }
    tip.visible = value.probe_state === "tracked" && Boolean(value.tip_w_m);
    if (value.tip_w_m) { tip.position.copy(worldToViewer(value.tip_w_m)); (tip.material as MeshBasicMaterial).color.set(value.quality === "good" ? 0x61e2b1 : value.quality === "warning" ? 0xf2bd55 : 0xff7479); }
  }

  setPaintData(delta: PaintDataDelta): void {
    if (!this.initialized || this.disposed) return;
    if (delta.reset) { disposeGroup(this.paint); this.paintObjects.clear(); }
    delta.removeIds?.forEach((id) => this.removePaint(id));
    delta.upsert?.forEach((record) => this.upsertPaint(record));
    delta.provisional?.forEach((record) => { this.removePaint(record.id); const marker = this.sphere(.0045, 0x58d6ff, .6); marker.position.copy(worldToViewer(record.position)); marker.userData = { recordType: "point", quality: record.quality }; this.paint.add(marker); this.paintObjects.set(record.id, marker); });
    this.visibility();
  }

  setFilters(value: ViewerFilters): void {
    this.filters = { ...this.filters, ...value }; if (value.pointBudget) this.budget = value.pointBudget;
    if (value.pointSize) this.map.traverse((object) => { if (object instanceof Points) object.material.size = value.pointSize!; }); this.visibility();
  }
  setSelection(value: ViewerSelection): void {
    this.paintObjects.forEach((object, id) => object.scale.setScalar(id === value.id ? 1.8 : 1));
    if (value.position && this.controls) { this.controls.target.copy(worldToViewer(value.position)); this.controls.update(); }
  }
  resize(width: number, height: number, dpr: number): void { if (!this.camera || !this.renderer || width < 1 || height < 1) return; this.camera.aspect = width / height; this.camera.updateProjectionMatrix(); this.dpr = Math.min(Math.max(.75, dpr), 2); this.renderer.setPixelRatio(this.dpr); this.renderer.setSize(width, height, false); }
  getMetrics(): ViewerMetrics { return { visiblePoints: this.map.visible ? this.loadedPoints : 0, loadedPoints: this.loadedPoints, loadedTiles: this.loadedTiles, drawCalls: this.renderer?.info.render.calls ?? 0, frameTimeMs: this.frameTime, pixelRatio: this.dpr, contextLost: this.contextLost }; }
  resetView(): void { if (!this.camera || !this.controls) return; this.camera.position.set(.45, .35, .55); this.controls.target.set(0,0,0); this.controls.update(); }
  dispose(): void {
    if (this.disposed) return; this.disposed = true; this.controller?.abort(); cancelAnimationFrame(this.frame); this.controls?.dispose();
    if (this.renderer) { this.renderer.domElement.removeEventListener("webglcontextlost", this.onLost); this.renderer.domElement.removeEventListener("webglcontextrestored", this.onRestored); this.renderer.dispose(); this.renderer.forceContextLoss(); this.renderer.domElement.remove(); }
    [this.map, this.registration, this.tracking, this.paint, this.helpers].forEach(disposeGroup); this.paintObjects.clear(); this.scene?.clear(); this.container = null; this.renderer = null; this.scene = null; this.camera = null; this.controls = null;
  }

  private loop = () => { if (this.disposed || !this.renderer || !this.scene || !this.camera) return; const now = performance.now(); this.frameTime = this.frameTime * .9 + (now - this.lastFrame) * .1; this.lastFrame = now; this.controls?.update(); if (!this.contextLost) this.renderer.render(this.scene, this.camera); this.frame = requestAnimationFrame(this.loop); };
  private onLost = (event: Event) => { event.preventDefault(); this.contextLost = true; };
  private onRestored = () => { this.contextLost = false; this.renderer?.info.reset(); };
  private visibility() { this.map.visible = this.filters.showMap !== false; this.helpers.visible = this.filters.showFrames !== false; this.registration.visible = this.mode === "registration" && this.filters.showBoard !== false; this.tracking.visible = this.mode === "live" && this.filters.showProbe !== false; this.paint.visible = this.mode === "live" || this.mode === "review"; this.paintObjects.forEach((object) => { const data = object.userData; object.visible = (data.recordType !== "point" || this.filters.showPoints !== false) && (data.recordType !== "path" || this.filters.showPaths !== false) && (!data.deleted || this.filters.includeDeleted === true) && (!this.filters.quality || this.filters.quality === "all" || data.quality === this.filters.quality); }); }
  private upsertPaint(record: PaintedPoint | PaintedPath) { this.removePaint(record.id); let object: Mesh | Line; if (record.type === "point") { object = this.sphere(.0045, record.deleted ? 0x667487 : record.quality === "good" ? 0x61e2b1 : record.quality === "warning" ? 0xf2bd55 : 0xff7479, record.deleted ? .35 : .95); object.position.copy(worldToViewer(record.position_w_m)); } else object = new Line(new BufferGeometry().setFromPoints(record.positions_w_m.map(worldToViewer)), new LineBasicMaterial({ color: record.deleted ? 0x667487 : 0x58d6ff, transparent: true, opacity: record.deleted ? .3 : .9 })); object.userData = { recordType: record.type, quality: record.quality, deleted: record.deleted }; this.paint.add(object); this.paintObjects.set(record.id, object); }
  private removePaint(id: string) { const object = this.paintObjects.get(id); if (!object) return; object.removeFromParent(); object.geometry.dispose(); const material = object.material; if (Array.isArray(material)) material.forEach((item) => item.dispose()); else material.dispose(); this.paintObjects.delete(id); }
  private sphere(radius: number, color: number, opacity = 1): Mesh { return new Mesh(new SphereGeometry(radius, 12, 8), new MeshBasicMaterial({ color, transparent: opacity < 1, opacity })); }
  private frameObject(group: Group) { if (!this.camera || !this.controls) return; const box = new Box3().setFromObject(group); if (box.isEmpty()) return; const size = box.getSize(new Vector3()); const center = box.getCenter(new Vector3()); const distance = Math.max(size.x, size.y, size.z, .1) * 1.8; this.controls.target.copy(center); this.camera.position.copy(center).add(new Vector3(distance, distance * .7, distance)); this.camera.near = Math.max(distance / 10000, .001); this.camera.far = Math.max(distance * 20, 20); this.camera.updateProjectionMatrix(); this.controls.update(); }
  private ensure() { if (!this.initialized || this.disposed) throw new Error("ViewerEngine is not available."); }
}
