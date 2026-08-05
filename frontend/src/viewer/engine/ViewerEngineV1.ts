import {
  AmbientLight, AxesHelper, Box3, BoxGeometry, BufferAttribute, BufferGeometry, Color, DirectionalLight, GridHelper, Group,
  Line, LineBasicMaterial, LineSegments, Matrix4, Mesh, MeshBasicMaterial, PerspectiveCamera, Points, PointsMaterial, Quaternion, Raycaster, Scene,
  SphereGeometry, SRGBColorSpace, Vector2, Vector3, WebGLRenderer,
} from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { TransformControls } from "three/addons/controls/TransformControls.js";
import type { PaintedPath, PaintedPoint, TrackingViewFrame } from "../../api/types";
import { decodeV1Tile, normalizeManifest, selectV1Tiles } from "../point-cloud/v1";
import type { CameraItem, MapTransformData, PaintDataDelta, PointCloudSource, RegistrationView, TransformMode, ViewerEngine as Contract, ViewerFilters, ViewerMetrics, ViewerMode, ViewerOptions, ViewerSelection } from "./types";

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
  private readonly transformPivot = new Group();
  private readonly camerasGroup = new Group();
  private readonly registration = new Group();
  private readonly tracking = new Group();
  private readonly paint = new Group();
  private readonly helpers = new Group();
  private readonly paintObjects = new Map<string, Mesh | Line>();
  private filters: ViewerFilters = { showMap: true, showFrames: true, showProbe: true, showBoard: true, showPoints: true, showPaths: true, pointSize: .012, pointBudget: 3_000_000 };
  public onCameraSelect?: (camera: CameraItem) => void;
  public onCameraDoubleClick?: ((camera: CameraItem) => void) | null;

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
    this.transformPivot.add(this.map);
    this.map.add(this.camerasGroup);
    this.scene.add(this.transformPivot, this.registration, this.tracking, this.paint, this.helpers, this.transformControls.getHelper(), new AmbientLight(0xa7b9cd, 1.3), new DirectionalLight(0xffffff, 1.7));
    const grid = new GridHelper(1, 20, 0x29415a, 0x162332); grid.material.opacity = .42; grid.material.transparent = true; this.helpers.add(grid, new AxesHelper(.08));
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

  private camScale = 1.0;

  setCameras(cameras: (CameraItem & { lookAt?: [number, number, number] })[]): void {
    disposeGroup(this.camerasGroup);
    if (this.camerasGroup.parent !== this.map) {
      this.map.add(this.camerasGroup);
    }
    if (!cameras || !cameras.length) return;
    const w = 0.06, h = 0.045, d = 0.10;
    const vertices = new Float32Array([
      0,0,0,  w,h,d,   0,0,0, -w,h,d,
      0,0,0, -w,-h,d,  0,0,0,  w,-h,d,
      w,h,d, -w,h,d,   -w,h,d, -w,-h,d,
      -w,-h,d, w,-h,d, w,-h,d,  w,h,d,
      0, -h, d,  0, -h - 0.03, d
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
    const matrix = this.map.matrixWorld.clone();
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
    this.map.position.set(0, 0, 0);
    this.map.quaternion.set(0, 0, 0, 1);
    this.map.scale.set(1, 1, 1);
    this.transformPivot.updateMatrixWorld(true);
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
    if (this.disposed) return; this.disposed = true; this.controller?.abort(); cancelAnimationFrame(this.frame); this.controls?.dispose(); this.transformControls?.dispose();
    if (this.onKeyDown) window.removeEventListener("keydown", this.onKeyDown);
    if (this.renderer) { this.renderer.domElement.removeEventListener("webglcontextlost", this.onLost); this.renderer.domElement.removeEventListener("webglcontextrestored", this.onRestored); this.renderer.dispose(); this.renderer.forceContextLoss(); this.renderer.domElement.remove(); }
    [this.map, this.registration, this.tracking, this.paint, this.helpers].forEach(disposeGroup); this.paintObjects.clear(); this.scene?.clear(); this.container = null; this.renderer = null; this.scene = null; this.camera = null; this.controls = null; this.transformControls = null;
  }

  private loop = () => { if (this.disposed || !this.renderer || !this.scene || !this.camera) return; const now = performance.now(); this.frameTime = this.frameTime * .9 + (now - this.lastFrame) * .1; this.lastFrame = now; this.controls?.update(); if (!this.contextLost) this.renderer.render(this.scene, this.camera); this.frame = requestAnimationFrame(this.loop); };
  private onLost = (event: Event) => { event.preventDefault(); this.contextLost = true; };
  private onRestored = () => { this.contextLost = false; this.renderer?.info.reset(); };
  private visibility() { this.map.visible = this.filters.showMap !== false; this.camerasGroup.visible = this.filters.showFrames !== false; this.helpers.visible = this.filters.showFrames !== false; this.registration.visible = this.mode === "registration" && this.filters.showBoard !== false; this.tracking.visible = this.mode === "live" && this.filters.showProbe !== false; this.paint.visible = this.mode === "live" || this.mode === "review"; this.paintObjects.forEach((object) => { const data = object.userData; object.visible = (data.recordType !== "point" || this.filters.showPoints !== false) && (data.recordType !== "path" || this.filters.showPaths !== false) && (!data.deleted || this.filters.includeDeleted === true) && (!this.filters.quality || this.filters.quality === "all" || data.quality === this.filters.quality); }); }
  private upsertPaint(record: PaintedPoint | PaintedPath) { this.removePaint(record.id); let object: Mesh | Line; if (record.type === "point") { object = this.sphere(.0045, record.deleted ? 0x667487 : record.quality === "good" ? 0x61e2b1 : record.quality === "warning" ? 0xf2bd55 : 0xff7479, record.deleted ? .35 : .95); object.position.copy(worldToViewer(record.position_w_m)); } else object = new Line(new BufferGeometry().setFromPoints(record.positions_w_m.map(worldToViewer)), new LineBasicMaterial({ color: record.deleted ? 0x667487 : 0x58d6ff, transparent: true, opacity: record.deleted ? .3 : .9 })); object.userData = { recordType: record.type, quality: record.quality, deleted: record.deleted }; this.paint.add(object); this.paintObjects.set(record.id, object); }
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
