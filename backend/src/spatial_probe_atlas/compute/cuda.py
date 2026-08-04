from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .profiles import CUDA_REQUIRED_DISTRIBUTIONS, ModelVerification, verify_model_assets


CUDA_WINDOWS_DRIVER_MINIMUM = (570, 65)
CUDA_RUNTIME_LINE = "12.8"
CUDA_MINIMUM_VRAM_BYTES = 4 * 1024**3


@dataclass(frozen=True, slots=True)
class CudaCapability:
    state: str
    available: bool
    reason_code: str | None = None
    reason: str | None = None
    driver_version: str | None = None
    device_name: str | None = None
    device_index: int | None = None
    compute_capability: str | None = None
    vram_bytes: int | None = None
    free_vram_bytes: int | None = None
    torch_version: str | None = None
    torch_cuda_build: str | None = None
    torch_arch_list: tuple[str, ...] = ()
    kernel_smoke: bool = False
    dependencies: dict[str, dict[str, Any]] = field(default_factory=dict)
    models: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["torch_arch_list"] = list(self.torch_arch_list)
        return value


def _version_tuple(value: str) -> tuple[int, ...]:
    result: list[int] = []
    for part in value.split("."):
        digits = "".join(character for character in part if character.isdigit())
        if not digits:
            break
        result.append(int(digits))
    return tuple(result)


def _nvidia_driver() -> tuple[str | None, str | None]:
    try:
        process = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version,name", "--format=csv,noheader,nounits"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if process.returncode != 0 or not process.stdout.strip():
            return None, None
        driver, _, name = process.stdout.splitlines()[0].partition(",")
        return driver.strip() or None, name.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None, None


def _installed_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in CUDA_REQUIRED_DISTRIBUTIONS:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _dependency_report(installed: dict[str, str | None]) -> tuple[dict[str, dict[str, Any]], bool]:
    report: dict[str, dict[str, Any]] = {}
    ready = True
    for name, required in CUDA_REQUIRED_DISTRIBUTIONS.items():
        observed = installed.get(name)
        valid = observed == required
        report[name] = {"required": required, "installed": observed, "verified": valid}
        ready &= valid
    return report, ready


def _result(
    state: str,
    available: bool,
    *,
    dependencies: dict[str, dict[str, Any]],
    models: ModelVerification,
    **values: Any,
) -> CudaCapability:
    return CudaCapability(
        state=state,
        available=available,
        dependencies=dependencies,
        models=models.as_dict(),
        **values,
    )


def probe_cuda(
    model_root: Path,
    *,
    manifest_path: Path | None = None,
    driver_probe: Callable[[], tuple[str | None, str | None]] = _nvidia_driver,
    installed_versions: dict[str, str | None] | None = None,
    torch_module: Any | None = None,
    kernel_probe: Callable[[Any], bool] | None = None,
) -> CudaCapability:
    """Run the complete non-fatal CUDA gate required before selection.

    The function never downloads a dependency or model. A successful result means
    the exact lock, driver/runtime, device, allocation/kernel, and model bytes all
    passed in this process.
    """

    driver_version, driver_device = driver_probe()
    installed = installed_versions or _installed_versions()
    dependencies, dependencies_ready = _dependency_report(installed)
    models = verify_model_assets(model_root, manifest_path=manifest_path)
    common = {"dependencies": dependencies, "models": models, "driver_version": driver_version, "device_name": driver_device}

    if driver_version is None:
        return _result("cpu_only", False, reason_code="NVIDIA_DRIVER_NOT_FOUND", reason="nvidia-smi did not report a device.", **common)
    if _version_tuple(driver_version) < CUDA_WINDOWS_DRIVER_MINIMUM:
        return _result(
            "cuda_incompatible",
            False,
            reason_code="CUDA_DRIVER_TOO_OLD",
            reason="CUDA 12.8 requires NVIDIA Windows driver 570.65 or newer.",
            **common,
        )
    if installed.get("torch") is None:
        return _result("cuda_driver_only", False, reason_code="PYTORCH_NOT_INSTALLED", reason="The NVIDIA driver is present but the CUDA PyTorch lock is not installed.", **common)
    if not dependencies_ready:
        return _result("cuda_incompatible", False, reason_code="CUDA_DEPENDENCY_MISMATCH", reason="Installed CUDA-profile packages do not match the immutable release profile.", **common)
    if not models.ready:
        return _result("cuda_incompatible", False, reason_code="CUDA_MODELS_INVALID", reason="One or more CUDA model files are missing or failed checksum verification.", **common)

    try:
        torch = torch_module or importlib.import_module("torch")
        torch_version = str(torch.__version__)
        torch_cuda_build = str(torch.version.cuda) if torch.version.cuda is not None else None
        if torch_cuda_build is None:
            return _result(
                "cuda_driver_only",
                False,
                reason_code="PYTORCH_CPU_BUILD",
                reason="The installed PyTorch build has no CUDA runtime.",
                torch_version=torch_version,
                **common,
            )
        if not torch_cuda_build.startswith(CUDA_RUNTIME_LINE):
            return _result(
                "cuda_incompatible",
                False,
                reason_code="PYTORCH_CUDA_RUNTIME_MISMATCH",
                reason=f"Expected CUDA {CUDA_RUNTIME_LINE}; PyTorch reports {torch_cuda_build}.",
                torch_version=torch_version,
                torch_cuda_build=torch_cuda_build,
                **common,
            )
        if not bool(torch.cuda.is_available()) or int(torch.cuda.device_count()) < 1:
            return _result(
                "cuda_incompatible",
                False,
                reason_code="PYTORCH_CUDA_UNAVAILABLE",
                reason="PyTorch could not initialize a CUDA device with the installed driver.",
                torch_version=torch_version,
                torch_cuda_build=torch_cuda_build,
                **common,
            )
        device_index = int(torch.cuda.current_device())
        properties = torch.cuda.get_device_properties(device_index)
        device_name = str(torch.cuda.get_device_name(device_index))
        major, minor = int(properties.major), int(properties.minor)
        capability = f"{major}.{minor}"
        arch_list = tuple(str(item) for item in torch.cuda.get_arch_list())
        # get_arch_list is an inventory, not a compatibility oracle: CUDA can run
        # same-major compatible cubins or embedded PTX on a newer minor capability
        # (for example sm_86 code on an sm_89 Ada device). The allocation/kernel
        # smoke below is the authoritative compatibility gate.
        total_vram = int(properties.total_memory)
        if total_vram < CUDA_MINIMUM_VRAM_BYTES:
            return _result(
                "cuda_incompatible",
                False,
                reason_code="CUDA_VRAM_INSUFFICIENT",
                reason="The CUDA mapping profile requires at least 4 GiB total VRAM.",
                device_name=device_name,
                device_index=device_index,
                compute_capability=capability,
                vram_bytes=total_vram,
                torch_version=torch_version,
                torch_cuda_build=torch_cuda_build,
                torch_arch_list=arch_list,
                **{key: value for key, value in common.items() if key != "device_name"},
            )
        if kernel_probe is None:
            left = torch.tensor([1.0, 2.0, 3.0], device=f"cuda:{device_index}")
            observed = float(torch.sum(left * left).detach().cpu().item())
            torch.cuda.synchronize(device_index)
            kernel_ok = observed == 14.0
        else:
            kernel_ok = bool(kernel_probe(torch))
        free_vram, _ = torch.cuda.mem_get_info(device_index)
        if not kernel_ok:
            raise RuntimeError("CUDA smoke kernel returned an invalid value")
        return _result(
            "cuda_ready",
            True,
            device_name=device_name,
            device_index=device_index,
            compute_capability=capability,
            vram_bytes=total_vram,
            free_vram_bytes=int(free_vram),
            torch_version=torch_version,
            torch_cuda_build=torch_cuda_build,
            torch_arch_list=arch_list,
            kernel_smoke=True,
            **{key: value for key, value in common.items() if key != "device_name"},
        )
    except Exception as exc:
        return _result(
            "degraded",
            False,
            reason_code="CUDA_SMOKE_FAILED",
            reason=f"{type(exc).__name__}: {exc}",
            **common,
        )


def is_cuda_out_of_memory(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return "outofmemory" in name or "out of memory" in message and "cuda" in message
