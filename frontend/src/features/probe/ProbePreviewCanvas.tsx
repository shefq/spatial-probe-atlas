import { useEffect, useRef, useState, useImperativeHandle, forwardRef } from "react";
import {
  AmbientLight,
  AxesHelper,
  Box3,
  BoxGeometry,
  BufferGeometry,
  CanvasTexture,
  Color,
  CylinderGeometry,
  DirectionalLight,
  DoubleSide,
  Float32BufferAttribute,
  GridHelper,
  Group,
  Line,
  LineBasicMaterial,
  LineSegments,
  Matrix4,
  Mesh,
  MeshBasicMaterial,
  MeshPhysicalMaterial,
  MeshStandardMaterial,
  PerspectiveCamera,
  Quaternion,
  Raycaster,
  Scene,
  SphereGeometry,
  Sprite,
  SpriteMaterial,
  Vector2,
  Vector3,
  WebGLRenderer,
} from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLExporter } from "three/addons/exporters/STLExporter.js";
import { DOT_LABELS, type ProbeDesignerConfig } from "./probeScriptGenerator";

export interface ProbeCanvasHandle {
  resetCamera: () => void;
  setFrontView: () => void;
  setSideView: () => void;
  setTopView: () => void;
  exportStlBinary: () => Uint8Array | null;
}

export interface ProbePreviewCanvasProps {
  config: ProbeDesignerConfig;
  wireframe?: boolean;
  xray?: boolean;
  showAxes?: boolean;
  showLabels?: boolean;
  showDimensions?: boolean;
  className?: string;
}

function makeTextSprite(text: string, colorHex: string, scale = 0.03): Sprite {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 64;
  const ctx = canvas.getContext("2d");
  if (ctx) {
    ctx.font = "Bold 28px sans-serif";
    ctx.fillStyle = colorHex;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, 128, 32);
  }
  const texture = new CanvasTexture(canvas);
  texture.needsUpdate = true;
  const sprite = new Sprite(new SpriteMaterial({ map: texture, depthTest: false }));
  sprite.scale.set(scale * 2.5, scale * 0.7, 1);
  return sprite;
}

export const ProbePreviewCanvas = forwardRef<ProbeCanvasHandle, ProbePreviewCanvasProps>(function ProbePreviewCanvas(
  { config, wireframe = false, xray = false, showAxes = true, showLabels = true, showDimensions = true, className = "" },
  ref
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<Scene | null>(null);
  const cameraRef = useRef<PerspectiveCamera | null>(null);
  const rendererRef = useRef<WebGLRenderer | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const probeRootRef = useRef<Group | null>(null);
  const exportableMeshGroupRef = useRef<Group | null>(null);
  const [activePresetView, setActivePresetView] = useState<"iso" | "front" | "side" | "top">("iso");

  // Imperative camera & STL export methods
  useImperativeHandle(ref, () => ({
    resetCamera: () => {
      if (!cameraRef.current || !controlsRef.current) return;
      cameraRef.current.position.set(0.18, -0.16, 0.12);
      controlsRef.current.target.set(0, 0, -0.04);
      controlsRef.current.update();
      setActivePresetView("iso");
    },
    setFrontView: () => {
      if (!cameraRef.current || !controlsRef.current) return;
      cameraRef.current.position.set(0.24, 0, -0.04);
      controlsRef.current.target.set(0, 0, -0.04);
      controlsRef.current.update();
      setActivePresetView("front");
    },
    setSideView: () => {
      if (!cameraRef.current || !controlsRef.current) return;
      cameraRef.current.position.set(0, 0.24, -0.04);
      controlsRef.current.target.set(0, 0, -0.04);
      controlsRef.current.update();
      setActivePresetView("side");
    },
    setTopView: () => {
      if (!cameraRef.current || !controlsRef.current) return;
      cameraRef.current.position.set(0, 0, 0.24);
      controlsRef.current.target.set(0, 0, -0.04);
      controlsRef.current.update();
      setActivePresetView("top");
    },
    exportStlBinary: () => {
      if (!exportableMeshGroupRef.current) return null;
      const exporter = new STLExporter();
      const stl = exporter.parse(exportableMeshGroupRef.current, { binary: true });
      return new Uint8Array(stl as unknown as ArrayBuffer);
    },
  }));

  // Initial Scene Setup
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const scene = new Scene();
    scene.background = new Color(0x0a1017);
    sceneRef.current = scene;

    const width = container.clientWidth || 600;
    const height = container.clientHeight || 480;

    const camera = new PerspectiveCamera(40, width / height, 0.001, 10.0);
    camera.position.set(0.18, -0.16, 0.12);
    cameraRef.current = camera;

    const renderer = new WebGLRenderer({ antialias: true, alpha: false });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    container.appendChild(renderer.domElement);
    rendererRef.current = renderer;

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.target.set(0, 0, -0.04);
    controls.minDistance = 0.02;
    controls.maxDistance = 1.0;
    controlsRef.current = controls;

    // Lighting
    const ambientLight = new AmbientLight(0xffffff, 1.2);
    scene.add(ambientLight);

    const keyLight = new DirectionalLight(0xffffff, 2.2);
    keyLight.position.set(0.3, -0.3, 0.4);
    scene.add(keyLight);

    const fillLight = new DirectionalLight(0x75a9e6, 1.5);
    fillLight.position.set(-0.3, 0.3, 0.2);
    scene.add(fillLight);

    const rimLight = new DirectionalLight(0x61e2b1, 1.0);
    rimLight.position.set(0, 0, -0.4);
    scene.add(rimLight);

    // Subtle Grid Helper
    const grid = new GridHelper(0.3, 30, 0x223548, 0x15212c);
    grid.rotation.x = Math.PI / 2;
    grid.position.z = -0.04;
    scene.add(grid);

    // Groups
    const probeRoot = new Group();
    scene.add(probeRoot);
    probeRootRef.current = probeRoot;

    let frameId: number;
    const animate = () => {
      frameId = requestAnimationFrame(animate);
      controls.update();
      renderer.render(scene, camera);
    };
    animate();

    const handleResize = () => {
      if (!container || !renderer || !camera) return;
      const w = container.clientWidth;
      const h = container.clientHeight;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
    };
    window.addEventListener("resize", handleResize);

    return () => {
      cancelAnimationFrame(frameId);
      window.removeEventListener("resize", handleResize);
      controls.dispose();
      renderer.dispose();
      if (container.contains(renderer.domElement)) {
        container.removeChild(renderer.domElement);
      }
    };
  }, []);

  // Rebuild 3D Probe Model when config or visual flags change
  useEffect(() => {
    const probeRoot = probeRootRef.current;
    if (!probeRoot) return;

    // Clean old children
    while (probeRoot.children.length > 0) {
      const obj = probeRoot.children[0];
      probeRoot.remove(obj);
      if (obj instanceof Mesh) {
        obj.geometry?.dispose();
        if (Array.isArray(obj.material)) obj.material.forEach((m) => m.dispose());
        else obj.material?.dispose();
      }
    }

    const exportGroup = new Group();
    probeRoot.add(exportGroup);
    exportableMeshGroupRef.current = exportGroup;

    // Coordinate conversions (mm -> m)
    const xRef = config.xRef / 1000.0;
    const dotsM = config.dotPositions.map(([x, y, z]) => new Vector3(x / 1000.0 + xRef, y / 1000.0, z / 1000.0));
    const sleeveLength = config.sleeveLength / 1000.0;
    const sleeveRadius = config.sleeveRadius / 1000.0;
    const probeLength = config.probeLength / 1000.0;
    const probeRadius = config.probeRadius / 1000.0;
    const probeZOffset = config.probeZOffset / 1000.0;
    const dotRadius = config.dotRadius / 1000.0;
    const backingRadius = config.backingPlateRadius / 1000.0;
    const armRadius = config.armRadius / 1000.0;
    const armWidth = config.armCenterWidth / 1000.0;
    const armEndWidth = config.armEndWidth / 1000.0;
    const armThickness = config.armCenterThickness / 1000.0;
    const armEndThickness = config.armEndThickness / 1000.0;

    // Materials
    const opacity = xray ? 0.35 : 1.0;
    const transparent = xray;

    const whiteMat = new MeshStandardMaterial({
      color: 0xe6e9ed,
      roughness: 0.35,
      metalness: 0.05,
      wireframe,
      transparent,
      opacity,
      side: DoubleSide,
    });

    const metalMat = new MeshStandardMaterial({
      color: 0x9ca3af,
      roughness: 0.2,
      metalness: 0.9,
      wireframe,
      transparent,
      opacity,
    });

    const rubyMat = new MeshPhysicalMaterial({
      color: 0xd92525,
      roughness: 0.1,
      metalness: 0.1,
      transmission: 0.4,
      ior: 1.76,
      wireframe,
    });

    const dotMat = new MeshBasicMaterial({
      color: 0x11161d,
      side: DoubleSide,
    });

    // 1. Central Sleeve
    const sleeveGeom = new CylinderGeometry(sleeveRadius, sleeveRadius, sleeveLength, 32);
    const sleeveMesh = new Mesh(sleeveGeom, whiteMat);
    sleeveMesh.position.set(0, 0, -sleeveLength / 2 + probeZOffset);
    sleeveMesh.rotation.x = Math.PI / 2;
    exportGroup.add(sleeveMesh);

    // 2. Probe Shaft
    const shaftGeom = new CylinderGeometry(probeRadius, probeRadius, probeLength, 32);
    const shaftMesh = new Mesh(shaftGeom, metalMat);
    shaftMesh.position.set(0, 0, probeZOffset - probeLength / 2);
    shaftMesh.rotation.x = Math.PI / 2;
    probeRoot.add(shaftMesh);

    // 3. Ruby Tip & GT Sphere
    const tipZ = probeZOffset - probeLength;
    const tipGeom = new SphereGeometry(0.005, 32, 32);
    const tipMesh = new Mesh(tipGeom, rubyMat);
    tipMesh.position.set(0, 0, tipZ);
    probeRoot.add(tipMesh);

    // 4. Backing Plates, Pegs, Arms, and Dots
    const xOffsetArm = -0.00325;
    const centerArmStart = new Vector3(dotsM[0].x + xOffsetArm, dotsM[0].y, dotsM[0].z);

    dotsM.forEach((dotPos, i) => {
      // Backing plate
      const plateGeom = new CylinderGeometry(backingRadius, backingRadius, 0.0015, 32);
      const plateMesh = new Mesh(plateGeom, whiteMat);
      plateMesh.position.set(dotPos.x - 0.00075, dotPos.y, dotPos.z);
      plateMesh.rotation.z = Math.PI / 2;
      exportGroup.add(plateMesh);

      // Tracking Dot
      const dotGeom = new CylinderGeometry(dotRadius, dotRadius, 0.0006, 32);
      const dotMesh = new Mesh(dotGeom, dotMat);
      dotMesh.position.set(dotPos.x + 0.0003, dotPos.y, dotPos.z);
      dotMesh.rotation.z = Math.PI / 2;
      probeRoot.add(dotMesh);

      // Peg
      const pegStart = new Vector3(dotPos.x + xOffsetArm, dotPos.y, dotPos.z);
      const pegEnd = new Vector3(dotPos.x - 0.0015, dotPos.y, dotPos.z);
      const pegLen = pegStart.distanceTo(pegEnd);
      if (pegLen > 1e-4) {
        const pegGeom = new CylinderGeometry(armRadius, armRadius, pegLen, 16);
        const pegMesh = new Mesh(pegGeom, whiteMat);
        pegMesh.position.copy(pegStart.clone().add(pegEnd).multiplyScalar(0.5));
        pegMesh.quaternion.setFromUnitVectors(new Vector3(0, 1, 0), pegEnd.clone().sub(pegStart).normalize());
        exportGroup.add(pegMesh);
      }

      // Radial Arm (for dots 1..4)
      if (i > 0) {
        const armEnd = new Vector3(dotPos.x + xOffsetArm, dotPos.y, dotPos.z);
        const armDir = armEnd.clone().sub(centerArmStart);
        const armLen = armDir.length();
        if (armLen > 1e-4) {
          // Tapered arm representation (smooth taper)
          // Three.js CylinderGeometry uses radiusTop and radiusBottom.
          // We scale it along X to match thickness, keeping Z as width.
          const armGeom = new CylinderGeometry(armEndWidth * 0.5, armWidth * 0.5, armLen, 32);
          // Scale X by thickness ratio relative to width, Z by 1 (which keeps the width as the radius base)
          armGeom.scale(armThickness / armWidth, 1, 1);
          
          const armMesh = new Mesh(armGeom, whiteMat);
          armMesh.position.copy(centerArmStart.clone().add(armEnd).multiplyScalar(0.5));
          armMesh.quaternion.setFromUnitVectors(new Vector3(0, 1, 0), armDir.clone().normalize());
          // We need to rotate it so the width (Z) aligns horizontally with the probe body.
          // By default, scaling X made it thin along X. So X is depth, Z is width.
          exportGroup.add(armMesh);
        }
      }

      // Labels
      if (showLabels) {
        const sprite = makeTextSprite(`[${i}]`, "#61e2b1", 0.02);
        sprite.position.set(dotPos.x + 0.006, dotPos.y, dotPos.z + 0.008);
        probeRoot.add(sprite);
      }
    });

    // 5. Origin Axes Helper
    if (showAxes) {
      const axes = new AxesHelper(0.04);
      probeRoot.add(axes);

      const labelOrigin = makeTextSprite("(0, 0, 0) Shaft Axis", "#f2bd55", 0.02);
      labelOrigin.position.set(0.005, 0.01, 0.005);
      probeRoot.add(labelOrigin);
    }

    // 6. Dimension Annotations
    if (showDimensions) {
      // Tip dimension line
      const dimPts = [new Vector3(0, 0, 0), new Vector3(0, 0, tipZ)];
      const dimGeom = new BufferGeometry().setFromPoints(dimPts);
      const dimLine = new Line(dimGeom, new LineBasicMaterial({ color: 0x536271 }));
      probeRoot.add(dimLine);

      const tipDistMm = Math.abs(config.probeZOffset - config.probeLength).toFixed(1);
      const dimLabel = makeTextSprite(`Tip Z: -${tipDistMm}mm`, "#61e2b1", 0.02);
      dimLabel.position.set(0.015, 0, tipZ / 2);
      probeRoot.add(dimLabel);
    }
  }, [config, wireframe, xray, showAxes, showLabels, showDimensions]);

  return (
    <div className={`probe-canvas-container ${className}`}>
      <div className="probe-canvas-viewport" ref={containerRef} />
      <div className="probe-canvas-controls">
        <div className="button-group button-group--sm">
          <button
            type="button"
            className={`button button--sm ${activePresetView === "iso" ? "button--primary" : "button--default"}`}
            onClick={() => {
              if (cameraRef.current && controlsRef.current) {
                cameraRef.current.position.set(0.18, -0.16, 0.12);
                controlsRef.current.target.set(0, 0, -0.04);
                controlsRef.current.update();
                setActivePresetView("iso");
              }
            }}
          >
            3D Iso
          </button>
          <button
            type="button"
            className={`button button--sm ${activePresetView === "front" ? "button--primary" : "button--default"}`}
            onClick={() => {
              if (cameraRef.current && controlsRef.current) {
                cameraRef.current.position.set(0.24, 0, -0.04);
                controlsRef.current.target.set(0, 0, -0.04);
                controlsRef.current.update();
                setActivePresetView("front");
              }
            }}
          >
            Front (YZ)
          </button>
          <button
            type="button"
            className={`button button--sm ${activePresetView === "side" ? "button--primary" : "button--default"}`}
            onClick={() => {
              if (cameraRef.current && controlsRef.current) {
                cameraRef.current.position.set(0, 0.24, -0.04);
                controlsRef.current.target.set(0, 0, -0.04);
                controlsRef.current.update();
                setActivePresetView("side");
              }
            }}
          >
            Side (XZ)
          </button>
          <button
            type="button"
            className={`button button--sm ${activePresetView === "top" ? "button--primary" : "button--default"}`}
            onClick={() => {
              if (cameraRef.current && controlsRef.current) {
                cameraRef.current.position.set(0, 0, 0.24);
                controlsRef.current.target.set(0, 0, -0.04);
                controlsRef.current.update();
                setActivePresetView("top");
              }
            }}
          >
            Top (XY)
          </button>
        </div>
      </div>
      <div className="probe-canvas-badge">
        <span>Tip Z: {((config.probeZOffset - config.probeLength)).toFixed(1)} mm</span>
        <span>•</span>
        <span>Shaft: {config.probeLength} mm</span>
        <span>•</span>
        <span>5 Markers</span>
      </div>
    </div>
  );
});
