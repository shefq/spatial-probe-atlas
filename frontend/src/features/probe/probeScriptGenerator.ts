/**
 * Pure generator utilities for Polaris Probe CAD and Calibration configuration.
 * All internal math handles conversion between user-friendly millimeters (mm)
 * and standard SI meters (m) used in Blender and Open3D / SPA.
 */

export interface DotCoordinate {
  x: number; // depth offset before xRef in mm
  y: number; // horizontal in mm
  z: number; // vertical in mm
  label: string;
}

export interface ProbeDesignerConfig {
  id: string;
  name: string;
  xRef: number; // mm (e.g. -5.0)
  dotPositions: [number, number, number][]; // mm [[X, Y, Z], ...]
  dotRadius: number; // mm (e.g. 2.5)
  backingPlateRadius: number; // mm (e.g. 5.0)
  markingRadius: number; // mm (e.g. 0.5)
  markingDepth: number; // mm (e.g. 0.5)
  probeLength: number; // mm (e.g. 100.0)
  probeRadius: number; // mm (e.g. 3.175)
  probeClearance: number; // mm (e.g. 0.15)
  probeZOffset: number; // mm (e.g. 10.0)
  sleeveLength: number; // mm (e.g. 40.0)
  sleeveRadius: number; // mm (e.g. 6.0)
  armRadius: number; // mm (e.g. 2.5)
  armCenterWidth: number; // mm (e.g. 12.0)
  armEndWidth: number; // mm (e.g. 6.0)
  armCenterThickness: number; // mm (e.g. 6.0)
  armEndThickness: number; // mm (e.g. 3.0)
  armCornerRadius: number; // mm (e.g. 1.5)
  armCornerSegments: number; // e.g. 16
  inspectMode: boolean;
  voxelSize: number; // mm (e.g. 0.3)
  autoExportStl: boolean;
  stlFilename: string;
}

export const DEFAULT_PROBE_CONFIG: ProbeDesignerConfig = {
  id: "polaris-probe-5dot",
  name: "Polaris 5-Marker Asymmetric Rigid Body Probe",
  xRef: -5.0,
  dotPositions: [
    [15.0, 0.0, 0.0],     // Center
    [5.0, -40.0, 45.0],   // Top-Left
    [-4.0, 45.0, 35.0],   // Top-Right
    [13.0, 0.0, -59.0],   // Bottom-Left
    [0.0, 35.0, -25.0],   // Bottom-Right
  ],
  dotRadius: 2.5,
  backingPlateRadius: 5.0,
  markingRadius: 0.5,
  markingDepth: 0.5,
  probeLength: 100.0,
  probeRadius: 3.175,
  probeClearance: 0.15,
  probeZOffset: 10.0,
  sleeveLength: 40.0,
  sleeveRadius: 6.0,
  armRadius: 2.5,
  armCenterWidth: 12.0,
  armEndWidth: 6.0,
  armCenterThickness: 6.0,
  armEndThickness: 3.0,
  armCornerRadius: 1.5,
  armCornerSegments: 16,
  inspectMode: false,
  voxelSize: 0.3,
  autoExportStl: true,
  stlFilename: "polaris_probe_fixture.stl",
};



export function generateRandomProbeId(prefix = "probe"): string {
  const hex = Math.random().toString(16).substring(2, 6);
  return `${prefix}-${hex}`;
}

export const DOT_LABELS = [
  "Center Marker (0)",
  "Top-Left (1)",
  "Top-Right (2)",
  "Bottom-Left (3)",
  "Bottom-Right (4)",
];

/**
 * Computes the 5 marker coordinates in meters, applying the global xRef.
 */
export function computeMarkerPointsMeters(config: ProbeDesignerConfig): number[][] {
  const xRefM = config.xRef / 1000.0;
  return config.dotPositions.map(([x, y, z]) => [
    x / 1000.0 + xRefM,
    y / 1000.0,
    z / 1000.0,
  ]);
}

/**
 * Computes tip position in local probe coordinates in meters [0, 0, Z_tip].
 */
export function computeTipPositionLocalMeters(config: ProbeDesignerConfig): [number, number, number] {
  const zTip = (config.probeZOffset - config.probeLength) / 1000.0;
  return [0.0, 0.0, zTip];
}

/**
 * Generates the complete, standalone Blender Python script that generates the
 * 3D printable fixture, shaft, tip, and optional STL export.
 */
export function generateBlenderScript(config: ProbeDesignerConfig): string {
  const xRefM = (config.xRef / 1000.0).toFixed(6);
  const dotRadiusM = (config.dotRadius / 1000.0).toFixed(6);
  const backingRadiusM = (config.backingPlateRadius / 1000.0).toFixed(6);
  const markingRadiusM = (config.markingRadius / 1000.0).toFixed(6);
  const markingDepthM = (config.markingDepth / 1000.0).toFixed(6);
  const probeLengthM = (config.probeLength / 1000.0).toFixed(6);
  const probeRadiusM = (config.probeRadius / 1000.0).toFixed(6);
  const probeClearanceM = (config.probeClearance / 1000.0).toFixed(6);
  const probeZOffsetM = (config.probeZOffset / 1000.0).toFixed(6);
  const sleeveLengthM = (config.sleeveLength / 1000.0).toFixed(6);
  const sleeveRadiusM = (config.sleeveRadius / 1000.0).toFixed(6);
  const armRadiusM = (config.armRadius / 1000.0).toFixed(6);
  const armCenterWidthM = (config.armCenterWidth / 1000.0).toFixed(6);
  const armEndWidthM = (config.armEndWidth / 1000.0).toFixed(6);
  const armCenterThicknessM = (config.armCenterThickness / 1000.0).toFixed(6);
  const armEndThicknessM = (config.armEndThickness / 1000.0).toFixed(6);
  const armCornerRadiusM = (config.armCornerRadius / 1000.0).toFixed(6);
  const voxelSizeM = (config.voxelSize / 1000.0).toFixed(6);
  const stlName = (config.stlFilename.trim() || `${config.id}.stl`).replace(/\\/g, '\\\\');

  const dotLines = config.dotPositions
    .map(([x, y, z], i) => {
      const xm = (x / 1000.0).toFixed(6);
      const ym = (y / 1000.0).toFixed(6);
      const zm = (z / 1000.0).toFixed(6);
      return `    (${xm} + X_REF, ${ym}, ${zm}),  # [${i}] ${DOT_LABELS[i] ?? `Dot ${i}`}`;
    })
    .join("\n");

  return `"""
==============================================================================
Spatial Probe Atlas · Procedural Polaris Rigid Body Probe Generator
Probe ID   : ${config.id}
Probe Name : ${config.name}
Generated  : ${new Date().toISOString()}
==============================================================================
How to run:
  1. Open Blender v4.5.
  2. Switch to the 'Scripting' tab in the top navigation bar.
  3. Click '+ New' text, paste this script, and click 'Run Script (▶)'.
  4. The 3D-printable rigid body will be automatically generated with smooth
     voxel fillets, shaft cavity, marker indents, and exported as '${stlName}'.
==============================================================================
"""

import bpy
import bmesh
import math
import mathutils
import os

# ==============================================================================
# PARAMETERS & CONFIGURATION (Generated from Spatial Probe Atlas)
# ==============================================================================
PROBE_ID   = "${config.id}"
PROBE_NAME = "${config.name.replace(/"/g, '\\"')}"

# Global depth reference offset (meters)
X_REF = ${xRefM}

# 5-Dot Constellation Coordinates (X=Depth, Y=Horizontal, Z=Vertical) in meters
DOT_POSITIONS = [
${dotLines}
]

# Tracking Dots & Backing Plates (meters)
DOT_RADIUS           = ${dotRadiusM}      # ${config.dotRadius} mm
BACKING_PLATE_RADIUS = ${backingRadiusM}  # ${config.backingPlateRadius} mm
MARKING_RADIUS       = ${markingRadiusM}  # ${config.markingRadius} mm center indent
MARKING_DEPTH        = ${markingDepthM}   # ${config.markingDepth} mm depth

# Probe Shaft & Sleeve Dimensions (meters)
PROBE_LENGTH    = ${probeLengthM}    # ${config.probeLength} mm probe shaft
PROBE_RADIUS    = ${probeRadiusM}    # ${config.probeRadius} mm radius (${(config.probeRadius * 2).toFixed(2)} mm diameter)
PROBE_CLEARANCE = ${probeClearanceM} # ${config.probeClearance} mm radial 3D-print tolerance
PROBE_Z_OFFSET  = ${probeZOffsetM}   # ${config.probeZOffset} mm offset above sleeve
SLEEVE_LENGTH   = ${sleeveLengthM}   # ${config.sleeveLength} mm central sleeve
SLEEVE_RADIUS   = ${sleeveRadiusM}   # ${config.sleeveRadius} mm outer radius
ARM_RADIUS      = ${armRadiusM}      # ${config.armRadius} mm cylindrical peg radius

# Tapered Radial Arms (meters)
ARM_CENTER_WIDTH     = ${armCenterWidthM}      # ${config.armCenterWidth} mm at center junction
ARM_END_WIDTH        = ${armEndWidthM}         # ${config.armEndWidth} mm near backing plates
ARM_CENTER_THICKNESS = ${armCenterThicknessM}  # ${config.armCenterThickness} mm at center
ARM_END_THICKNESS    = ${armEndThicknessM}     # ${config.armEndThickness} mm at dot ends
ARM_CORNER_RADIUS    = ${armCornerRadiusM}     # ${config.armCornerRadius} mm corner rounding
ARM_CORNER_SEGMENTS  = ${config.armCornerSegments}

# Remeshing & Export Options
INSPECT_MODE     = ${config.inspectMode ? "True" : "False"}
VOXEL_SIZE       = ${voxelSizeM}     # ${config.voxelSize} mm high-resolution remesh
AUTO_EXPORT_STL  = ${config.autoExportStl ? "True" : "False"}
STL_FILENAME     = "${stlName}"

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================
def clean_scene():
    """Clear existing objects, meshes, materials, and lights."""
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()
    for block in [bpy.data.meshes, bpy.data.materials, bpy.data.cameras, bpy.data.lights]:
        for item in block:
            block.remove(item)

def create_material(name, color, metallic=0.0, roughness=0.5):
    """Create a principled BSDF shader material."""
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs['Base Color'].default_value = color
        bsdf.inputs['Metallic'].default_value = metallic
        bsdf.inputs['Roughness'].default_value = roughness
    return mat

def create_arm(p1, p2, radius, material, parent_obj, name="Arm"):
    """Create a cylindrical arm connecting point p1 to p2."""
    v1 = mathutils.Vector(p1)
    v2 = mathutils.Vector(p2)
    direction = v2 - v1
    length = direction.length
    if length == 0:
        return None
    midpoint = (v1 + v2) / 2.0
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=64, radius=radius, depth=length, location=midpoint
    )
    arm = bpy.context.active_object
    arm.name = name
    arm.parent = parent_obj
    arm.data.materials.append(material)
    bpy.ops.object.shade_smooth()
    rot_quat = mathutils.Vector((0, 0, 1)).rotation_difference(direction.normalized())
    arm.rotation_euler = rot_quat.to_euler()
    return arm

def _rounded_rectangle_ring(center, depth_axis, side_axis, width, thickness, corner_radius, corner_segments):
    """Generate a rounded-rectangle vertex loop."""
    half_depth = thickness / 2.0
    half_width = width / 2.0
    radius = min(corner_radius, half_depth, half_width)
    segments = max(1, int(corner_segments))

    corners = (
        ( half_depth - radius,  half_width - radius, 0.0),
        (-half_depth + radius,  half_width - radius, math.pi / 2.0),
        (-half_depth + radius, -half_width + radius, math.pi),
        ( half_depth - radius, -half_width + radius, 3.0 * math.pi / 2.0),
    )

    ring = []
    for depth_center, side_center, start_angle in corners:
        for step in range(segments + 1):
            angle = start_angle + (math.pi / 2.0) * step / segments
            depth = depth_center + radius * math.cos(angle)
            side = side_center + radius * math.sin(angle)
            ring.append(center + depth_axis * depth + side_axis * side)
    return ring

def create_tapered_rounded_rect_arm(
    p1, p2, center_width, end_width, center_thickness, end_thickness,
    corner_radius, material, parent_obj, name="Arm",
):
    """Create a conical tapered arm with rounded-rectangle profile."""
    start = mathutils.Vector(p1)
    end = mathutils.Vector(p2)
    direction = end - start
    if direction.length == 0:
        return None

    arm_axis = direction.normalized()
    depth_axis = mathutils.Vector((1.0, 0.0, 0.0))
    depth_axis -= arm_axis * depth_axis.dot(arm_axis)
    if depth_axis.length < 1e-6:
        depth_axis = mathutils.Vector((0.0, 1.0, 0.0))
        depth_axis -= arm_axis * depth_axis.dot(arm_axis)
    depth_axis.normalize()
    side_axis = arm_axis.cross(depth_axis).normalized()

    start_ring = _rounded_rectangle_ring(start, depth_axis, side_axis, center_width, center_thickness, corner_radius, ARM_CORNER_SEGMENTS)
    end_ring = _rounded_rectangle_ring(end, depth_axis, side_axis, end_width, end_thickness, corner_radius, ARM_CORNER_SEGMENTS)

    vertices = [tuple(v) for v in start_ring + end_ring]
    ring_size = len(start_ring)
    faces = []

    for i in range(ring_size):
        next_i = (i + 1) % ring_size
        faces.append((i, next_i, ring_size + next_i, ring_size + i))

    faces.append(tuple(reversed(range(ring_size))))
    faces.append(tuple(range(ring_size, ring_size * 2)))

    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()

    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()

    arm = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(arm)
    arm.parent = parent_obj
    arm.data.materials.append(material)
    for polygon in mesh.polygons:
        polygon.use_smooth = len(polygon.vertices) == 4
    return arm

# ==============================================================================
# MAIN PROBE BUILDER
# ==============================================================================
def build_probe_assembly():
    """Build the full 3D assembly and printable manifold rigid body."""
    # 1. Materials
    mat_white = create_material("Mat_WhiteBase",  (0.92, 0.92, 0.92, 1.0), roughness=0.6)
    mat_black = create_material("Mat_BlackDot",   (0.01, 0.01, 0.01, 1.0), roughness=0.8)
    mat_metal = create_material("Mat_MetalShaft", (0.70, 0.70, 0.70, 1.0), metallic=0.9, roughness=0.25)
    mat_tip   = create_material("Mat_RubyTip",    (0.85, 0.08, 0.08, 1.0), metallic=0.1, roughness=0.08)

    # 2. Main Assembly Empty Root
    bpy.ops.object.empty_add(type='ARROWS', radius=0.05)
    assembly = bpy.context.active_object
    assembly.name = f"ProbeAssembly_{PROBE_ID}"

    printed_parts = []
    x_offset = -0.00325

    # 3. Arms connecting Center to each Dot
    for i in range(1, len(DOT_POSITIONS)):
        p1 = (DOT_POSITIONS[0][0] + x_offset, DOT_POSITIONS[0][1], DOT_POSITIONS[0][2])
        p2 = (DOT_POSITIONS[i][0] + x_offset, DOT_POSITIONS[i][1], DOT_POSITIONS[i][2])
        arm = create_tapered_rounded_rect_arm(
            p1, p2,
            ARM_CENTER_WIDTH, ARM_END_WIDTH,
            ARM_CENTER_THICKNESS, ARM_END_THICKNESS,
            ARM_CORNER_RADIUS, mat_white, assembly, name=f"Arm_{i}",
        )
        if arm: printed_parts.append(arm)

    # 4. Pegs
    for i, pos in enumerate(DOT_POSITIONS):
        p_back  = (pos[0] + x_offset, pos[1], pos[2])
        p_front = (pos[0] - 0.0015, pos[1], pos[2])
        peg = create_arm(p_back, p_front, ARM_RADIUS, mat_white, assembly, name=f"Peg_{i}")
        if peg: printed_parts.append(peg)

    # 5. Central Sleeve
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=128, radius=SLEEVE_RADIUS, depth=SLEEVE_LENGTH, location=(0, 0, -SLEEVE_LENGTH/2 + PROBE_Z_OFFSET)
    )
    sleeve = bpy.context.active_object
    sleeve.name = "Sleeve"
    sleeve.parent = assembly
    sleeve.data.materials.append(mat_white)
    bpy.ops.object.shade_smooth()
    printed_parts.append(sleeve)

    # 6. Backing plates
    for i, pos in enumerate(DOT_POSITIONS):
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=128, radius=BACKING_PLATE_RADIUS, depth=0.0015,
            location=(pos[0] - 0.00075, pos[1], pos[2]),
            rotation=(0, math.radians(90), 0)
        )
        plate = bpy.context.active_object
        plate.name = f"DotBacking_{i}"
        plate.parent = assembly
        plate.data.materials.append(mat_white)
        bpy.ops.object.shade_smooth()
        printed_parts.append(plate)

    rigid_body = None
    if not INSPECT_MODE:
        # Join into single manifold
        bpy.ops.object.select_all(action='DESELECT')
        for p in printed_parts:
            p.select_set(True)
        bpy.context.view_layer.objects.active = sleeve
        bpy.ops.object.join()
        rigid_body = bpy.context.active_object
        rigid_body.name = f"RigidBody_{PROBE_ID}"

        # Remesh for smooth printable fillets
        remesh = rigid_body.modifiers.new(name="Remesh", type='REMESH')
        remesh.mode = 'VOXEL'
        remesh.voxel_size = VOXEL_SIZE
        remesh.use_smooth_shade = True
        bpy.ops.object.modifier_apply(modifier="Remesh")

        # Cut probe shaft bore hole
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=128, radius=PROBE_RADIUS + PROBE_CLEARANCE, depth=SLEEVE_LENGTH + 0.020,
            location=(0, 0, -SLEEVE_LENGTH/2 + PROBE_Z_OFFSET)
        )
        hole = bpy.context.active_object
        bool_mod = rigid_body.modifiers.new(name="Hole", type='BOOLEAN')
        bool_mod.operation = 'DIFFERENCE'
        bool_mod.object = hole
        bpy.context.view_layer.objects.active = rigid_body
        bpy.ops.object.modifier_apply(modifier="Hole")
        bpy.data.objects.remove(hole)

        # Cut alignment markings for stickers
        for i, pos in enumerate(DOT_POSITIONS):
            bpy.ops.mesh.primitive_cylinder_add(
                vertices=32, radius=MARKING_RADIUS, depth=MARKING_DEPTH * 2,
                location=(pos[0], pos[1], pos[2]),
                rotation=(0, math.radians(90), 0)
            )
            mark = bpy.context.active_object
            bool_mod = rigid_body.modifiers.new(name=f"Mark_{i}", type='BOOLEAN')
            bool_mod.operation = 'DIFFERENCE'
            bool_mod.object = mark
            bpy.context.view_layer.objects.active = rigid_body
            bpy.ops.object.modifier_apply(modifier=f"Mark_{i}")
            bpy.data.objects.remove(mark)

    # 7. Tracking Dots
    for i, pos in enumerate(DOT_POSITIONS):
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=128, radius=DOT_RADIUS, depth=0.0005,
            location=(pos[0] + 0.00025, pos[1], pos[2]),
            rotation=(0, math.radians(90), 0)
        )
        dot = bpy.context.active_object
        dot.name = f"Dot_{i}"
        dot.parent = assembly
        dot.data.materials.append(mat_black)

    # 8. Probe Shaft
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=64, radius=PROBE_RADIUS, depth=PROBE_LENGTH,
        location=(0, 0, PROBE_Z_OFFSET - (PROBE_LENGTH / 2.0))
    )
    shaft = bpy.context.active_object
    shaft.name = "ProbeShaft"
    shaft.parent = assembly
    shaft.data.materials.append(mat_metal)
    bpy.ops.object.shade_smooth()

    # 9. Ruby Tip & Ground Truth
    tip_z = PROBE_Z_OFFSET - PROBE_LENGTH
    bpy.ops.mesh.primitive_uv_sphere_add(
        segments=64, ring_count=32, radius=0.005, location=(0, 0, tip_z)
    )
    tip = bpy.context.active_object
    tip.name = "ProbeTip_Ruby"
    tip.parent = assembly
    tip.data.materials.append(mat_tip)
    bpy.ops.object.shade_smooth()

    bpy.ops.object.empty_add(type='SPHERE', radius=0.005, location=(0, 0, tip_z))
    tip_gt = bpy.context.active_object
    tip_gt.name = "ProbeTip_GroundTruth"
    tip_gt.parent = assembly

    # Initial view position
    assembly.location = (0.05, -0.05, 0.2)
    assembly.rotation_euler = (math.radians(15), math.radians(-10), math.radians(25))

    # Optional STL Export
    if AUTO_EXPORT_STL and rigid_body:
        export_path = STL_FILENAME
        bpy.ops.object.select_all(action='DESELECT')
        rigid_body.select_set(True)
        bpy.context.view_layer.objects.active = rigid_body
        try:
            bpy.ops.wm.stl_export(filepath=export_path, export_selected_objects=True)
            print(f"Exported STL to: {export_path}")
        except Exception as e:
            print(f"STL auto-export skipped: {e}")

    print(f"=== Polaris Probe [{PROBE_ID}] built successfully! ===")
    print(f"Tip Z offset: {tip_z*1000:.1f} mm | Shaft length: {PROBE_LENGTH*1000:.1f} mm")

if __name__ == "__main__":
    clean_scene()
    build_probe_assembly()
`;
}

/**
 * Generates Spatial Probe Atlas compatible Probe Calibration JSON.
 */
export function generateCalibrationJson(config: ProbeDesignerConfig): string {
  const markerPointsM = computeMarkerPointsMeters(config);
  const [tipX, tipY, tipZ] = computeTipPositionLocalMeters(config);

  // 4x4 Transformation Matrix from Marker Frame M to Probe Tip Frame P
  // Identity rotation with translation [tipX, tipY, tipZ]
  const tMarkerTip = [
    1.0, 0.0, 0.0, tipX,
    0.0, 1.0, 0.0, tipY,
    0.0, 0.0, 1.0, tipZ,
    0.0, 0.0, 0.0, 1.0,
  ];

  const payload = {
    id: config.id,
    name: config.name,
    created_at: new Date().toISOString(),
    probe: {
      model: "polaris_5_blob",
      marker_frame: "M",
      tip_frame: "P",
      marker_points_m: markerPointsM,
      t_marker_tip: tMarkerTip,
      metadata: {
        x_ref_mm: config.xRef,
        dot_radius_mm: config.dotRadius,
        shaft_length_mm: config.probeLength,
        shaft_radius_mm: config.probeRadius,
        sleeve_length_mm: config.sleeveLength,
        sleeve_radius_mm: config.sleeveRadius,
        tip_z_offset_mm: config.probeZOffset - config.probeLength,
      },
    },
  };

  return JSON.stringify(payload, null, 2);
}
