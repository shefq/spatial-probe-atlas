import os
import subprocess
import shutil
from pathlib import Path
from typing import Callable, Any

from spatial_probe_atlas.domain.errors import AppError


ProgressCallback = Callable[[str, int, int, float, str], None]


def _run_mvs(command: list[str], cwd: Path, progress: ProgressCallback, stage: str, stage_index: int, total_stages: int, message: str, bin_path: Path | None) -> None:
    progress(stage, stage_index, total_stages, 0.0, message)
    
    executable = command[0]
    if bin_path:
        if os.name == "nt" and not executable.endswith(".exe"):
            executable += ".exe"
        executable = str(bin_path / executable)
        command = [executable] + command[1:]

    try:
        subprocess.run(
            command,
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise AppError(
            f"OPENMVS_{command[0].upper()}_FAILED",
            f"{command[0]} failed: {exc.stdout}",
            status_code=500,
        )
    except FileNotFoundError:
        raise AppError(
            "OPENMVS_NOT_FOUND",
            f"'{executable}' executable not found. Ensure OpenMVS is installed and the path is correct.",
            status_code=500,
        )
    progress(stage, stage_index, total_stages, 1.0, f"{command[0]} completed successfully.")


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
    import shutil
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
        ["ReconstructMesh", "model_dense.mvs", "-o", "model_dense_mesh.mvs", "-w", str(workspace.resolve())],
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
        ["TextureMesh", "model_dense.mvs", "-m", "model_dense_mesh.ply", "--export-type", "obj", "-w", str(workspace.resolve())],
        cwd=workspace,
        progress=progress,
        stage="texture_mesh",
        stage_index=4,
        total_stages=4,
        message="Texturing mesh",
        bin_path=openmvs_bin,
    )
    
    final_obj = workspace / "model_dense_texture.obj"
    if not final_obj.exists():
        final_obj = workspace / "model_dense_mesh_texture.obj"
    
    if not final_obj.exists():
         raise AppError("OPENMVS_MESH_NOT_FOUND", "The expected output mesh was not found.", status_code=500)
    
    # Move the final mesh to the map_dir root for easy access
    for f in workspace.glob(f"{final_obj.stem}.*"):
        if f.suffix == ".obj":
            shutil.copy(f, map_dir / "mesh.obj")
        else:
            shutil.copy(f, map_dir / f.name)
            
    # Optional: cleanup workspace to save space
    # shutil.rmtree(workspace, ignore_errors=True)

    return {
        "mesh_path": "mesh.obj",
    }
