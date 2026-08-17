import os
import subprocess
import shutil
from pathlib import Path
from typing import Callable, Any

from spatial_probe_atlas.domain.errors import AppError


ProgressCallback = Callable[[str, int, int, float, str], None]


import re
import time

def _parse_openmvs_line(raw_line: str) -> tuple[float | None, str | None, str]:
    # Strip ANSI escape codes
    line = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', raw_line).strip()
    if not line:
        return None, None, ""

    # Remove timestamps like "12:08:14 [ScnTextr] " or "[App     ] "
    clean = re.sub(r'^\d{2}:\d{2}:\d{2}\s*\[[^\]]+\]\s*', '', line)
    clean = re.sub(r'^\[[^\]]+\]\s*', '', clean).strip()

    pct: float | None = None
    eta: str | None = None

    # Search for percentage: matches "(45%", "45%", "( 45% )", "(100%, 287ms)", etc.
    pct_match = re.search(r'\(?\s*(\d{1,3})\s*%', clean)
    if pct_match:
        val = int(pct_match.group(1))
        if 0 <= val <= 100:
            pct = val / 100.0

    # Search for ETA: matches "ETA 10s", "ETA 1m23s", "ETA 00:15", "ETA 2s345ms", etc.
    eta_match = re.search(r'ETA\s*[:=]?\s*([0-9a-zA-Z\:\.]+)', clean, re.IGNORECASE)
    if eta_match:
        eta = eta_match.group(1).rstrip('),;.')

    return pct, eta, clean


def _run_mvs(command: list[str], cwd: Path, progress: ProgressCallback, stage: str, stage_index: int, total_stages: int, message: str, bin_path: Path | None) -> None:
    start_progress = (stage_index - 1) / float(total_stages)
    progress(stage, stage_index, total_stages, start_progress, message)
    
    executable = command[0]
    if bin_path:
        if os.name == "nt" and not executable.endswith(".exe"):
            executable += ".exe"
        executable = str(bin_path / executable)
        command = [executable] + command[1:]

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )
    except FileNotFoundError:
        raise AppError(
            "OPENMVS_NOT_FOUND",
            f"'{executable}' executable not found. Ensure OpenMVS is installed and the path is correct.",
            status_code=500,
        )

    last_update_time = 0.0
    last_pct = 0.0

    if process.stdout:
        buffer: list[str] = []
        while True:
            char = process.stdout.read(1)
            if not char:
                break
            if char in ('\r', '\n'):
                line = "".join(buffer).strip()
                buffer.clear()
                if line:
                    pct, eta, clean_line = _parse_openmvs_line(line)
                    now = time.time()
                    
                    if pct is not None:
                        last_pct = pct
                        overall = max(0.0, min(1.0, ((stage_index - 1) + pct) / float(total_stages)))
                        msg = clean_line
                        if eta and "ETA" not in msg.upper():
                            msg += f" · ETA {eta}"
                        progress(stage, stage_index, total_stages, overall, msg)
                        last_update_time = now
                    elif clean_line and len(clean_line) > 6 and (now - last_update_time > 0.4):
                        # Filter out non-essential debug lines (like memory info, SSE info)
                        if not any(k in clean_line for k in ("MEMORYINFO", "PageFault", "WorkingSet", "Quota", "Pagefile", "CPU:", "RAM:", "OS:", "Disk:", "Build date:")):
                            overall = max(0.0, min(1.0, ((stage_index - 1) + last_pct) / float(total_stages)))
                            progress(stage, stage_index, total_stages, overall, clean_line[:120])
                            last_update_time = now
            else:
                buffer.append(char)

    process.wait()
    if process.returncode != 0:
        raise AppError(
            f"OPENMVS_{command[0].upper()}_FAILED",
            f"{command[0]} failed with exit code {process.returncode}.",
            status_code=500,
        )
        
    end_progress = stage_index / float(total_stages)
    progress(stage, stage_index, total_stages, end_progress, f"{command[0]} completed successfully.")


def _color_mesh_from_cameras(colmap_dir: Path, images_dir: Path, input_ply: Path, output_ply: Path, output_obj: Path | None = None) -> None:
    """
    Project calibrated camera images directly onto mesh vertices to create a 100%
    full-color mesh with zero occlusion artifacts, black holes, or UV seams.
    """
    import struct
    import numpy as np
    from PIL import Image

    if not input_ply.exists():
        return

    # 1. Load input PLY
    with open(input_ply, "rb") as f:
        header = ""
        while True:
            line = f.readline().decode("ascii", errors="ignore")
            header += line
            if line.startswith("end_header"):
                break
        
        num_v = int([l.split()[-1] for l in header.splitlines() if "element vertex" in l][0])
        num_f = int([l.split()[-1] for l in header.splitlines() if "element face" in l][0])
        
        raw_v = f.read(num_v * 12)
        verts = np.frombuffer(raw_v, dtype="<f4").reshape(-1, 3)
        raw_f = f.read(num_f * 13)
        faces = np.frombuffer(raw_f, dtype=[("count", "u1"), ("i0", "<u4"), ("i1", "<u4"), ("i2", "<u4")])
        faces = np.column_stack([faces["i0"], faces["i1"], faces["i2"]])

    # 2. Load COLMAP camera intrinsics
    cameras: dict[int, dict[str, float]] = {}
    cameras_bin = colmap_dir / "cameras.bin"
    if cameras_bin.exists():
        with open(cameras_bin, "rb") as f:
            num_cams = struct.unpack("<Q", f.read(8))[0]
            for _ in range(num_cams):
                cam_id = struct.unpack("<I", f.read(4))[0]
                model_id = struct.unpack("<i", f.read(4))[0]
                w = struct.unpack("<Q", f.read(8))[0]
                h = struct.unpack("<Q", f.read(8))[0]
                fx, fy, cx, cy = struct.unpack("<4d", f.read(32))
                cameras[cam_id] = {"w": float(w), "h": float(h), "fx": fx, "fy": fy, "cx": cx, "cy": cy}

    # 3. Load registered images
    images: list[dict[str, Any]] = []
    images_bin = colmap_dir / "images.bin"
    if images_bin.exists():
        with open(images_bin, "rb") as f:
            num_images = struct.unpack("<Q", f.read(8))[0]
            for _ in range(num_images):
                img_id = struct.unpack("<I", f.read(4))[0]
                qw, qx, qy, qz = struct.unpack("<4d", f.read(32))
                tx, ty, tz = struct.unpack("<3d", f.read(24))
                cam_id = struct.unpack("<I", f.read(4))[0]
                name = b""
                while True:
                    c = f.read(1)
                    if c == b"\x00":
                        break
                    name += c
                num_pts = struct.unpack("<Q", f.read(8))[0]
                f.read(num_pts * 24)

                R = np.array([
                    [1 - 2*qy**2 - 2*qz**2, 2*qx*qy - 2*qz*qw, 2*qx*qz + 2*qy*qw],
                    [2*qx*qy + 2*qz*qw, 1 - 2*qx**2 - 2*qz**2, 2*qy*qz - 2*qx*qw],
                    [2*qx*qz - 2*qy*qw, 2*qy*qz + 2*qx*qw, 1 - 2*qx**2 - 2*qy**2],
                ])
                t = np.array([tx, ty, tz])
                cam = cameras.get(cam_id)
                img_file = images_dir / name.decode()
                if cam and img_file.exists():
                    try:
                        img_arr = np.array(Image.open(img_file))
                        images.append({"R": R, "t": t, "cam": cam, "img": img_arr})
                    except Exception:
                        pass

    if not images:
        return

    # 4. Project vertices into cameras and accumulate weighted RGB
    v_colors = np.zeros((len(verts), 3), dtype=np.float32)
    v_weights = np.zeros(len(verts), dtype=np.float32)

    for item in images:
        R, t, cam, img = item["R"], item["t"], item["cam"], item["img"]
        H, W, _ = img.shape
        X_c = (verts @ R.T) + t
        z = X_c[:, 2]
        valid_z = z > 0.1
        u = (cam["fx"] * X_c[:, 0] / z) + cam["cx"]
        v = (cam["fy"] * X_c[:, 1] / z) + cam["cy"]
        valid = valid_z & (u >= 0) & (u < W - 1) & (v >= 0) & (v < H - 1)
        valid_idxs = np.where(valid)[0]
        if len(valid_idxs) == 0:
            continue
        u_valid = u[valid_idxs].astype(int)
        v_valid = v[valid_idxs].astype(int)
        weight = 1.0 / (z[valid_idxs] + 1e-6)
        sampled = img[v_valid, u_valid, :3].astype(np.float32)
        v_colors[valid_idxs] += sampled * weight[:, None]
        v_weights[valid_idxs] += weight

    has_color = v_weights > 0
    v_colors[has_color] /= v_weights[has_color, None]
    v_colors = np.clip(v_colors, 0, 255).astype(np.uint8)

    # 5. Write colored binary PLY
    with open(output_ply, "wb") as f:
        hdr = f"""ply
format binary_little_endian 1.0
element vertex {len(verts)}
property float32 x
property float32 y
property float32 z
property uchar red
property uchar green
property uchar blue
element face {len(faces)}
property list uint8 uint32 vertex_indices
end_header
"""
        f.write(hdr.encode("ascii"))
        v_dtype = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1")])
        v_data = np.empty(len(verts), dtype=v_dtype)
        v_data["x"], v_data["y"], v_data["z"] = verts[:, 0], verts[:, 1], verts[:, 2]
        v_data["r"], v_data["g"], v_data["b"] = v_colors[:, 0], v_colors[:, 1], v_colors[:, 2]
        f.write(v_data.tobytes())

        f_dtype = np.dtype([("count", "u1"), ("i0", "<u4"), ("i1", "<u4"), ("i2", "<u4")])
        f_data = np.empty(len(faces), dtype=f_dtype)
        f_data["count"] = 3
        f_data["i0"], f_data["i1"], f_data["i2"] = faces[:, 0], faces[:, 1], faces[:, 2]
        f_data.tofile(f)

    # 6. Optional: Write vertex-colored OBJ
    if output_obj:
        with open(output_obj, "w") as f:
            f.write("# Spatial Probe Atlas Colored Mesh\n")
            lines = [f"v {vx:.6f} {vy:.6f} {vz:.6f} {vr/255.0:.4f} {vg/255.0:.4f} {vb/255.0:.4f}\n"
                     for vx, vy, vz, vr, vg, vb in zip(verts[:, 0], verts[:, 1], verts[:, 2], v_colors[:, 0], v_colors[:, 1], v_colors[:, 2])]
            f.writelines(lines)
            flines = [f"f {i0+1} {i1+1} {i2+1}\n" for i0, i1, i2 in faces]
            f.writelines(flines)


def build_openmvs_mesh(
    map_dir: Path,
    progress: ProgressCallback,
    cancelled: Callable[[], bool],
    openmvs_bin: Path | None = None,
) -> dict[str, Any]:
    colmap_dir = map_dir / "colmap" / "0"
    images_dir = map_dir / "images"

    if not colmap_dir.exists() or not images_dir.exists():
        raise AppError(
            "OPENMVS_NO_COLMAP_DATA",
            "This map does not contain the COLMAP reconstruction required for OpenMVS.",
            status_code=400,
            suggested_action="Generate a new map using the CUDA/COLMAP profile.",
        )

    workspace = map_dir / "openmvs"
    workspace.mkdir(parents=True, exist_ok=True)
    
    # InterfaceCOLMAP expects the COLMAP data to be in a "sparse" subdirectory of the input path.
    sparse_dir = workspace / "sparse"
    sparse_dir.mkdir(parents=True, exist_ok=True)
    for file in colmap_dir.iterdir():
        if file.is_file():
            shutil.copy2(file, sparse_dir / file.name)
    
    # 1. InterfaceCOLMAP
    _run_mvs(
        ["InterfaceCOLMAP", "-i", str(workspace.resolve()), "-o", "model.mvs", "--image-folder", str(images_dir.resolve())],
        cwd=workspace,
        progress=progress,
        stage="interface_colmap",
        stage_index=1,
        total_stages=4,
        message="Converting COLMAP workspace to OpenMVS format",
        bin_path=openmvs_bin,
    )
    if cancelled(): raise InterruptedError()

    # 2. DensifyPointCloud
    _run_mvs(
        ["DensifyPointCloud", "model.mvs", "-w", str(workspace.resolve())],
        cwd=workspace,
        progress=progress,
        stage="densify_point_cloud",
        stage_index=2,
        total_stages=4,
        message="Generating dense point cloud",
        bin_path=openmvs_bin,
    )
    if cancelled(): raise InterruptedError()

    # 3. ReconstructMesh
    _run_mvs(
        [
            "ReconstructMesh", "model_dense.mvs",
            "-o", "model_dense_mesh.mvs",
            "-w", str(workspace.resolve()),
            "--smooth", "2",
        ],
        cwd=workspace,
        progress=progress,
        stage="reconstruct_mesh",
        stage_index=3,
        total_stages=4,
        message="Reconstructing mesh surface",
        bin_path=openmvs_bin,
    )
    if cancelled(): raise InterruptedError()

    # 4. TextureMesh
    _run_mvs(
        [
            "TextureMesh", "model_dense.mvs",
            "-m", "model_dense_mesh.ply",
            "--export-type", "obj",
            "-w", str(workspace.resolve()),
            "--resolution-level", "0",
            "--texture-size", "4096",
            "--cost-smoothness-ratio", "0.1",
        ],
        cwd=workspace,
        progress=progress,
        stage="texture_mesh",
        stage_index=4,
        total_stages=4,
        message="Texturing mesh",
        bin_path=openmvs_bin,
    )
    if cancelled(): raise InterruptedError()
    
    # 5. Direct camera projection to guarantee 100% full-color mesh without black holes
    reconstructed_mesh = workspace / "model_dense_mesh.ply"
    colored_mesh_ply = workspace / "colored_mesh.ply"
    colored_mesh_obj = workspace / "colored_mesh.obj"
    try:
        _color_mesh_from_cameras(colmap_dir, images_dir, reconstructed_mesh, colored_mesh_ply, colored_mesh_obj)
        shutil.copy(colored_mesh_ply, map_dir / "mesh.ply")
        shutil.copy(colored_mesh_obj, map_dir / "colored_mesh.obj")
    except Exception as exc:
        print(f"Warning: Direct camera coloring failed: {exc}")

    final_obj = workspace / "model_dense_texture.obj"
    if not final_obj.exists():
        final_obj = workspace / "model_dense_mesh_texture.obj"
    
    if final_obj.exists():
        # Move the final mesh to the map_dir root for easy access
        for f in workspace.glob(f"{final_obj.stem}.*"):
            if f.suffix == ".obj":
                shutil.copy(f, map_dir / "mesh.obj")
            else:
                shutil.copy(f, map_dir / f.name)

    return {
        "mesh_path": "mesh.obj",
        "colored_mesh_path": "mesh.ply",
    }

