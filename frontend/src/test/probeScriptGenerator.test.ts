import { describe, expect, it } from "vitest";
import {
  DEFAULT_PROBE_CONFIG,
  computeMarkerPointsMeters,
  computeTipPositionLocalMeters,
  generateBlenderScript,
  generateCalibrationJson,
  generateRandomProbeId,
  type ProbeDesignerConfig,
} from "../features/probe/probeScriptGenerator";

describe("probeScriptGenerator", () => {
  it("computes marker points in meters with xRef offset applied", () => {
    const pts = computeMarkerPointsMeters(DEFAULT_PROBE_CONFIG);
    expect(pts).toHaveLength(5);

    // Center dot: X=15mm, xRef=-5mm -> (15 - 5)mm = 10mm = 0.010m
    expect(pts[0][0]).toBeCloseTo(0.010, 5);
    expect(pts[0][1]).toBeCloseTo(0.000, 5);
    expect(pts[0][2]).toBeCloseTo(0.000, 5);

    // Top-Left dot: X=5mm, Y=-40mm, Z=45mm, xRef=-5mm -> (5-5)=0mm
    expect(pts[1][0]).toBeCloseTo(0.000, 5);
    expect(pts[1][1]).toBeCloseTo(-0.040, 5);
    expect(pts[1][2]).toBeCloseTo(0.045, 5);
  });

  it("computes tip local position accurately", () => {
    // probeZOffset = 10mm, probeLength = 100mm -> Tip Z = 10 - 100 = -90mm = -0.090m
    const tip = computeTipPositionLocalMeters(DEFAULT_PROBE_CONFIG);
    expect(tip[0]).toBe(0);
    expect(tip[1]).toBe(0);
    expect(tip[2]).toBeCloseTo(-0.090, 5);
  });

  it("generates a valid Blender Python script with custom ID and parameters", () => {
    const customConfig: ProbeDesignerConfig = {
      ...DEFAULT_PROBE_CONFIG,
      id: "my-custom-probe-99",
      name: "Custom Probe 99",
      probeLength: 120.0,
      stlFilename: "custom_probe_99.stl",
    };

    const script = generateBlenderScript(customConfig);
    expect(script).toContain('PROBE_ID   = "my-custom-probe-99"');
    expect(script).toContain('PROBE_NAME = "Custom Probe 99"');
    expect(script).toContain('PROBE_LENGTH    = 0.120000');
    expect(script).toContain('STL_FILENAME     = "custom_probe_99.stl"');
    expect(script).toContain('build_probe_assembly()');
    expect(script).toContain('clean_scene()');
  });

  it("generates valid Spatial Probe Atlas Calibration JSON", () => {
    const jsonStr = generateCalibrationJson(DEFAULT_PROBE_CONFIG);
    const parsed = JSON.parse(jsonStr);

    expect(parsed.id).toBe(DEFAULT_PROBE_CONFIG.id);
    expect(parsed.name).toBe(DEFAULT_PROBE_CONFIG.name);
    expect(parsed.probe.model).toBe("polaris_5_blob");
    expect(parsed.probe.marker_points_m).toHaveLength(5);
    expect(parsed.probe.t_marker_tip).toHaveLength(16);

    // Verify 4x4 matrix translation
    const tMatrix = parsed.probe.t_marker_tip;
    expect(tMatrix[3]).toBe(0);
    expect(tMatrix[7]).toBe(0);
    expect(tMatrix[11]).toBeCloseTo(-0.090, 5); // Z translation in 4th column of row 3
    expect(tMatrix[15]).toBe(1);
  });

  it("generates random probe IDs", () => {
    const id1 = generateRandomProbeId("probe");
    const id2 = generateRandomProbeId("probe");
    expect(id1).toMatch(/^probe-[a-f0-9]{4}$/);
    expect(id1).not.toBe(id2);
  });
});
