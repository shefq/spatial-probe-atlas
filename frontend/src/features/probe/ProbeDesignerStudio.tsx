import { useMemo, useRef, useState } from "react";
import {
  DEFAULT_PROBE_CONFIG,
  DOT_LABELS,
  generateBlenderScript,
  generateCalibrationJson,
  generateRandomProbeId,
  type ProbeDesignerConfig,
} from "./probeScriptGenerator";
import { ProbePreviewCanvas, type ProbeCanvasHandle } from "./ProbePreviewCanvas";
import { Button, Card, Field, InlineAlert, Metric, TextInput, Toggle, Modal } from "../../components/ui";
import { useUiStore } from "../../stores";

export function ProbeDesignerStudio() {
  const pushToast = useUiStore((state) => state.pushToast);
  const canvasRef = useRef<ProbeCanvasHandle>(null);

  const [config, setConfig] = useState<ProbeDesignerConfig>(() => ({
    ...DEFAULT_PROBE_CONFIG,
    id: generateRandomProbeId("polaris-probe"),
  }));

  const [activeControlTab, setActiveControlTab] = useState<"dots" | "shaft" | "arms" | "export">("dots");
  const [activeViewTab, setActiveViewTab] = useState<"3d" | "script" | "json">("3d");

  // Visual Viewport Flags
  const [wireframe, setWireframe] = useState(false);
  const [xray, setXray] = useState(false);
  const [showAxes, setShowAxes] = useState(true);
  const [showLabels, setShowLabels] = useState(true);
  const [showDimensions, setShowDimensions] = useState(true);

  // Modals
  const [epnpModalOpen, setEpnpModalOpen] = useState(false);

  // Generated Outputs
  const blenderScript = useMemo(() => generateBlenderScript(config), [config]);
  const calibrationJson = useMemo(() => generateCalibrationJson(config), [config]);

  // Handlers
  const handleUpdateConfig = <K extends keyof ProbeDesignerConfig>(key: K, value: ProbeDesignerConfig[K]) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  };

  const handleUpdateDot = (index: number, axis: 0 | 1 | 2, val: number) => {
    setConfig((prev) => {
      const nextDots = prev.dotPositions.map((dot, i) => {
        if (i !== index) return dot;
        const next = [...dot] as [number, number, number];
        next[axis] = isNaN(val) ? 0 : val;
        return next;
      });
      return { ...prev, dotPositions: nextDots };
    });
  };



  const handleRegenerateId = () => {
    const newId = generateRandomProbeId("polaris-probe");
    handleUpdateConfig("id", newId);
    handleUpdateConfig("stlFilename", `${newId}.stl`);
    pushToast({ kind: "info", title: "Generated new Probe ID", message: newId });
  };

  const handleCopyScript = async () => {
    try {
      await navigator.clipboard.writeText(blenderScript);
      pushToast({ kind: "success", title: "Blender script copied!", message: "Paste into Blender's Scripting workspace and press Run Script (▶)." });
    } catch {
      pushToast({ kind: "error", title: "Copy failed", message: "Could not write to clipboard." });
    }
  };

  const handleDownloadScript = () => {
    const blob = new Blob([blenderScript], { type: "text/x-python;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `create_${config.id.replace(/[^a-zA-Z0-9_-]/g, "_")}.py`;
    link.click();
    URL.revokeObjectURL(url);
    pushToast({ kind: "success", title: "Python script downloaded", message: link.download });
  };

  const handleDownloadJson = () => {
    const blob = new Blob([calibrationJson], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${config.id}_calibration.json`;
    link.click();
    URL.revokeObjectURL(url);
    pushToast({ kind: "success", title: "Calibration JSON downloaded", message: link.download });
  };



  const handleResetDefaults = () => {
    setConfig({
      ...DEFAULT_PROBE_CONFIG,
      id: config.id,
    });
    pushToast({ kind: "info", title: "Reset to default parameters" });
  };

  const tipZMm = (config.probeZOffset - config.probeLength).toFixed(1);

  return (
    <div className="probe-designer-studio">
      {/* Top Header & Identity Card */}
      <header className="probe-designer-header">
        <div className="probe-designer-identity">
          <div className="probe-id-row">
            <Field label="Probe Identifier (ID)" hint="Unique machine ID used in code, calibrations & STL files">
              <div className="input-with-action">
                <TextInput
                  value={config.id}
                  onChange={(e) => handleUpdateConfig("id", e.target.value)}
                  placeholder="e.g. polaris-probe-01"
                  className="font-mono"
                />
                <Button size="sm" onClick={handleRegenerateId} title="Generate random ID">🎲 New ID</Button>
              </div>
            </Field>
            <Field label="Probe Display Name" hint="Human-readable probe description">
              <TextInput
                value={config.name}
                onChange={(e) => handleUpdateConfig("name", e.target.value)}
                placeholder="e.g. Surgical 5-Dot Asymmetric Probe"
              />
            </Field>
          </div>
        </div>
      </header>

      {/* Main Studio Grid: Left Parameters / Right 3D & Script View */}
      <div className="probe-designer-grid">
        {/* Left Column: Parameter Editor */}
        <aside className="probe-params-sidebar">
          <nav className="probe-params-tabs" aria-label="Parameter categories">
            <button
              type="button"
              className={`probe-tab-button ${activeControlTab === "dots" ? "is-active" : ""}`}
              onClick={() => setActiveControlTab("dots")}
            >
              1. Dots & Markers
            </button>
            <button
              type="button"
              className={`probe-tab-button ${activeControlTab === "shaft" ? "is-active" : ""}`}
              onClick={() => setActiveControlTab("shaft")}
            >
              2. Shaft & Sleeve
            </button>
            <button
              type="button"
              className={`probe-tab-button ${activeControlTab === "arms" ? "is-active" : ""}`}
              onClick={() => setActiveControlTab("arms")}
            >
              3. Arms & Fillets
            </button>
            <button
              type="button"
              className={`probe-tab-button ${activeControlTab === "export" ? "is-active" : ""}`}
              onClick={() => setActiveControlTab("export")}
            >
              4. Export & Remesh
            </button>
          </nav>

          <div className="probe-params-body">
            {/* TAB 1: DOTS & MARKERS */}
            {activeControlTab === "dots" && (
              <div className="form-stack">
                <div style={{ marginBottom: "16px" }}>
                  <InlineAlert
                    tone="info"
                    title="EPnP Tracking Accuracy Guide"
                    action={<Button size="sm" onClick={() => setEpnpModalOpen(true)}>Read Guide</Button>}
                  >
                    Learn how dot placement geometry affects 3D tracking.
                  </InlineAlert>
                </div>

                <Card title="Global Depth Reference (X_REF)" eyebrow="RIGID BODY PLANE">
                  <Field label="X_REF offset (mm)" hint="Global depth shift added to all marker X values (default -5.0 mm)">
                    <TextInput
                      type="number"
                      step="0.5"
                      value={config.xRef}
                      onChange={(e) => handleUpdateConfig("xRef", parseFloat(e.target.value) || 0)}
                    />
                  </Field>
                </Card>

                <Card title="5-Dot Constellation Coordinates (mm)" eyebrow="LOCAL FIXTURE FRAME">
                  <p className="muted small">
                    X = Depth offset before X_REF, Y = Horizontal (+Right / -Left), Z = Vertical (+Up / -Down).
                  </p>
                  <div className="dots-coord-list">
                    {config.dotPositions.map((pos, idx) => (
                      <div key={idx} className="dot-coord-card">
                        <div className="dot-coord-header">
                          <span className="dot-badge">{idx}</span>
                          <strong>{DOT_LABELS[idx]}</strong>
                        </div>
                        <div className="dot-coord-inputs">
                          <label>
                            <span>X (depth)</span>
                            <TextInput
                              type="number"
                              step="0.5"
                              value={pos[0]}
                              onChange={(e) => handleUpdateDot(idx, 0, parseFloat(e.target.value))}
                            />
                          </label>
                          <label>
                            <span>Y (horiz)</span>
                            <TextInput
                              type="number"
                              step="0.5"
                              value={pos[1]}
                              onChange={(e) => handleUpdateDot(idx, 1, parseFloat(e.target.value))}
                            />
                          </label>
                          <label>
                            <span>Z (vert)</span>
                            <TextInput
                              type="number"
                              step="0.5"
                              value={pos[2]}
                              onChange={(e) => handleUpdateDot(idx, 2, parseFloat(e.target.value))}
                            />
                          </label>
                        </div>
                      </div>
                    ))}
                  </div>
                </Card>

                <Card title="Marker & Backing Plate Radii" eyebrow="DIMENSIONS (mm)">
                  <div className="form-grid-2">
                    <Field label="Dot radius (mm)" hint="Tracking sticker radius (e.g. 2.5 mm)">
                      <TextInput
                        type="number"
                        step="0.1"
                        min="1"
                        value={config.dotRadius}
                        onChange={(e) => handleUpdateConfig("dotRadius", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Backing plate radius (mm)" hint="White plastic backing pad">
                      <TextInput
                        type="number"
                        step="0.1"
                        min="2"
                        value={config.backingPlateRadius}
                        onChange={(e) => handleUpdateConfig("backingPlateRadius", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Marking indent radius (mm)" hint="Center alignment dimple">
                      <TextInput
                        type="number"
                        step="0.1"
                        value={config.markingRadius}
                        onChange={(e) => handleUpdateConfig("markingRadius", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Marking depth (mm)" hint="Alignment cutout depth">
                      <TextInput
                        type="number"
                        step="0.1"
                        value={config.markingDepth}
                        onChange={(e) => handleUpdateConfig("markingDepth", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                  </div>
                </Card>
              </div>
            )}

            {/* TAB 2: SHAFT & SLEEVE */}
            {activeControlTab === "shaft" && (
              <div className="form-stack">
                <Card title="Probe Shaft Geometry" eyebrow="METAL / STEEL SHAFT">
                  <div className="form-grid-2">
                    <Field label="Shaft length (mm)" hint="Total metal shaft length (e.g. 100 mm)">
                      <TextInput
                        type="number"
                        step="5"
                        min="20"
                        value={config.probeLength}
                        onChange={(e) => handleUpdateConfig("probeLength", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Shaft radius (mm)" hint="Shaft outer radius (3.175 mm = 1/4 inch diameter)">
                      <TextInput
                        type="number"
                        step="0.1"
                        min="1"
                        value={config.probeRadius}
                        onChange={(e) => handleUpdateConfig("probeRadius", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Shaft Z Offset (mm)" hint="Height above top sleeve opening (default 10 mm)">
                      <TextInput
                        type="number"
                        step="1"
                        value={config.probeZOffset}
                        onChange={(e) => handleUpdateConfig("probeZOffset", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="3D Print Radial Clearance (mm)" hint="Tolerance gap for shaft hole (0.15 mm)">
                      <TextInput
                        type="number"
                        step="0.05"
                        min="0.0"
                        value={config.probeClearance}
                        onChange={(e) => handleUpdateConfig("probeClearance", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                  </div>
                  <div className="metric-strip">
                    <Metric label="Calculated Tip Z" value={`${tipZMm} mm`} tone="good" />
                    <Metric label="Bore Diameter" value={`${((config.probeRadius + config.probeClearance) * 2).toFixed(2)} mm`} />
                  </div>
                </Card>

                <Card title="Central Sleeve Geometry" eyebrow="3D PRINTED HOUSING">
                  <div className="form-grid-2">
                    <Field label="Sleeve length (mm)" hint="Central collar length (e.g. 40 mm)">
                      <TextInput
                        type="number"
                        step="5"
                        min="15"
                        value={config.sleeveLength}
                        onChange={(e) => handleUpdateConfig("sleeveLength", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Sleeve outer radius (mm)" hint="Outer collar radius (e.g. 6.0 mm)">
                      <TextInput
                        type="number"
                        step="0.5"
                        min="3"
                        value={config.sleeveRadius}
                        onChange={(e) => handleUpdateConfig("sleeveRadius", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Peg / Stalk radius (mm)" hint="Peg connecting arms to backing plates">
                      <TextInput
                        type="number"
                        step="0.1"
                        min="1"
                        value={config.armRadius}
                        onChange={(e) => handleUpdateConfig("armRadius", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                  </div>
                </Card>
              </div>
            )}

            {/* TAB 3: ARMS & FILLETS */}
            {activeControlTab === "arms" && (
              <div className="form-stack">
                <Card title="Tapered Arm Profile" eyebrow="RADIAL STRUCTURE">
                  <p className="muted small">
                    Arms taper smoothly from the central sleeve junction out to each tracking dot backing plate.
                  </p>
                  <div className="form-grid-2">
                    <Field label="Center junction width (mm)" hint="Width at central collar (12 mm)">
                      <TextInput
                        type="number"
                        step="0.5"
                        min="4"
                        value={config.armCenterWidth}
                        onChange={(e) => handleUpdateConfig("armCenterWidth", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Dot end width (mm)" hint="Width near backing plate (6 mm)">
                      <TextInput
                        type="number"
                        step="0.5"
                        min="2"
                        value={config.armEndWidth}
                        onChange={(e) => handleUpdateConfig("armEndWidth", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Center thickness (mm)" hint="Front-to-back thickness at center (6 mm)">
                      <TextInput
                        type="number"
                        step="0.5"
                        min="2"
                        value={config.armCenterThickness}
                        onChange={(e) => handleUpdateConfig("armCenterThickness", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Dot end thickness (mm)" hint="Front-to-back thickness at end (3 mm)">
                      <TextInput
                        type="number"
                        step="0.5"
                        min="1"
                        value={config.armEndThickness}
                        onChange={(e) => handleUpdateConfig("armEndThickness", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Corner radius (mm)" hint="Rounded rectangle corner fillet (1.5 mm)">
                      <TextInput
                        type="number"
                        step="0.1"
                        min="0.5"
                        value={config.armCornerRadius}
                        onChange={(e) => handleUpdateConfig("armCornerRadius", parseFloat(e.target.value) || 0)}
                      />
                    </Field>
                    <Field label="Corner smoothness segments" hint="Ring subdivision segments (16)">
                      <TextInput
                        type="number"
                        step="2"
                        min="4"
                        max="32"
                        value={config.armCornerSegments}
                        onChange={(e) => handleUpdateConfig("armCornerSegments", parseInt(e.target.value, 10) || 16)}
                      />
                    </Field>
                  </div>
                </Card>
              </div>
            )}

            {/* TAB 4: EXPORT & REMESH */}
            {activeControlTab === "export" && (
              <div className="form-stack">
                <Card title="Blender Remesh & 3D Print Settings" eyebrow="MANIFOLD MESHING">
                  <Field label="Voxel remesh size (mm)" hint="Voxel grid resolution in Blender (0.3 mm = smooth fillets)">
                    <TextInput
                      type="number"
                      step="0.05"
                      min="0.1"
                      value={config.voxelSize}
                      onChange={(e) => handleUpdateConfig("voxelSize", parseFloat(e.target.value) || 0.3)}
                    />
                  </Field>
                  <Field label="Output STL Full Path" hint="Target full absolute path for auto-export in Blender (e.g., C:\path\to\export.stl)">
                    <TextInput
                      value={config.stlFilename}
                      onChange={(e) => handleUpdateConfig("stlFilename", e.target.value)}
                    />
                  </Field>
                  <div className="toggle-stack">
                    <Toggle
                      label="Auto-export STL when script runs in Blender"
                      checked={config.autoExportStl}
                      onChange={(e) => handleUpdateConfig("autoExportStl", e.target.checked)}
                    />
                    <Toggle
                      label="Inspect mode (skip remesh/boolean to inspect individual parts)"
                      checked={config.inspectMode}
                      onChange={(e) => handleUpdateConfig("inspectMode", e.target.checked)}
                    />
                  </div>
                </Card>

                <Card title="Blender Quick Instructions" eyebrow="HOW TO RUN">
                  <ol className="step-list">
                    <li>Open Blender v4.5.</li>
                    <li>Click the <strong>Scripting</strong> tab at the top.</li>
                    <li>Click <strong>+ New</strong>, paste the copied script, and click <strong>Run Script (▶)</strong>.</li>
                    <li>Blender joins all parts, applies smooth voxel fillets, bores the shaft cavity, and exports <code>{config.stlFilename}</code>!</li>
                  </ol>
                </Card>
              </div>
            )}
          </div>

          <footer className="probe-params-footer">
            <Button size="sm" variant="ghost" onClick={handleResetDefaults}>↺ Reset to Defaults</Button>
          </footer>
        </aside>

        {/* Right Column: 3D Viewport / Python Script / JSON Preview */}
        <section className="probe-viewport-panel">
          <div className="probe-viewport-toolbar">
            <div className="button-group">
              <button
                type="button"
                className={`button button--sm ${activeViewTab === "3d" ? "button--primary" : "button--default"}`}
                onClick={() => setActiveViewTab("3d")}
              >
                🎮 3D Studio View
              </button>
              <button
                type="button"
                className={`button button--sm ${activeViewTab === "script" ? "button--primary" : "button--default"}`}
                onClick={() => setActiveViewTab("script")}
              >
                🐍 Blender Python Script
              </button>
              <button
                type="button"
                className={`button button--sm ${activeViewTab === "json" ? "button--primary" : "button--default"}`}
                onClick={() => setActiveViewTab("json")}
              >
                📄 Calibration JSON
              </button>
            </div>

            <div className="probe-viewport-actions">
              <Button variant="primary" size="sm" onClick={handleCopyScript}>📋 Copy Blender Script</Button>
              <Button size="sm" onClick={handleDownloadScript}>💾 Download .py</Button>

              <Button size="sm" onClick={handleDownloadJson}>📄 Export JSON</Button>
            </div>
          </div>

          <div className="probe-viewport-content">
            {activeViewTab === "3d" && (
              <div className="probe-3d-wrapper">
                <ProbePreviewCanvas
                  ref={canvasRef}
                  config={config}
                  wireframe={wireframe}
                  xray={xray}
                  showAxes={showAxes}
                  showLabels={showLabels}
                  showDimensions={showDimensions}
                />
                {/* 3D Viewport Floating Overlay Controls */}
                <div className="probe-viewport-overlay">
                  <div className="overlay-pill">
                    <label>
                      <input type="checkbox" checked={wireframe} onChange={(e) => setWireframe(e.target.checked)} />
                      <span>Wireframe</span>
                    </label>
                    <label>
                      <input type="checkbox" checked={xray} onChange={(e) => setXray(e.target.checked)} />
                      <span>X-Ray</span>
                    </label>
                    <label>
                      <input type="checkbox" checked={showAxes} onChange={(e) => setShowAxes(e.target.checked)} />
                      <span>Axes</span>
                    </label>
                    <label>
                      <input type="checkbox" checked={showLabels} onChange={(e) => setShowLabels(e.target.checked)} />
                      <span>Labels</span>
                    </label>
                    <label>
                      <input type="checkbox" checked={showDimensions} onChange={(e) => setShowDimensions(e.target.checked)} />
                      <span>Dimensions</span>
                    </label>
                  </div>
                </div>
              </div>
            )}

            {activeViewTab === "script" && (
              <div className="probe-code-view">
                <div className="code-view-banner">
                  <span>Python script updates dynamically with all parameter changes.</span>
                  <Button size="sm" variant="primary" onClick={handleCopyScript}>Copy to Clipboard</Button>
                </div>
                <pre className="code-block">
                  <code>{blenderScript}</code>
                </pre>
              </div>
            )}

            {activeViewTab === "json" && (
              <div className="probe-code-view">
                <div className="code-view-banner">
                  <span>Spatial Probe Atlas Calibration JSON schema with computed <code>marker_points_m</code> & <code>t_marker_tip</code>.</span>
                  <Button size="sm" variant="primary" onClick={handleDownloadJson}>Download JSON</Button>
                </div>
                <pre className="code-block font-mono">
                  <code>{calibrationJson}</code>
                </pre>
              </div>
            )}
          </div>
        </section>
      </div>

      <Modal open={epnpModalOpen} onRequestClose={() => setEpnpModalOpen(false)} title="EPnP Design Best Practices" size="lg">
        <div className="prose">
          <p>When designing a rigid body probe, you are designing the 3D points for the EPnP (Efficient Perspective-n-Point) tracking algorithm. To maximize rotational tracking accuracy and robustness, follow these geometric rules:</p>
          <ul>
            <li><strong>3D Volume (Non-Coplanar)</strong>: Avoid putting all dots on a flat plane. Varying the <code>X (depth)</code> creates a 3D bounding box that mathematically prevents depth ambiguity (flipping).</li>
            <li><strong>Geometric Asymmetry</strong>: The arrangement must be irregular. If the shape is symmetric (like a square), the camera cannot uniquely determine the probe's orientation. Offset the <code>Y</code> and <code>Z</code> coordinates so no two arms are identical.</li>
            <li><strong>Spatial Spread (Baseline)</strong>: The further apart the dots are physically (max <code>Y</code> and <code>Z</code> distances), the smaller the rotational error caused by sub-pixel noise in the camera. Make the constellation as large as practically possible.</li>
            <li><strong>Avoid Collinearity</strong>: No 3 dots should lie on a perfect straight line, which weakens the geometric constraint matrix.</li>
            <li><strong>Redundancy</strong>: EPnP strictly requires 4 points for unambiguous pose. By using 5 points, the system becomes over-constrained, allowing it to mathematically average out noise and survive temporary occlusions of one dot.</li>
          </ul>
        </div>
      </Modal>
    </div>
  );
}
