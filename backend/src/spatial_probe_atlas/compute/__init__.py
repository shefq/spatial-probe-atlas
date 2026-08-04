"""Verified compute-profile selection and capability probes."""

from .cuda import CudaCapability, probe_cuda
from .profiles import (
    CPU_MAPPING_PROFILE,
    CUDA_MAPPING_PROFILE,
    REPLAY_MAPPING_PROFILE,
    resolve_mapping_profile,
)

__all__ = [
    "CPU_MAPPING_PROFILE",
    "CUDA_MAPPING_PROFILE",
    "REPLAY_MAPPING_PROFILE",
    "CudaCapability",
    "probe_cuda",
    "resolve_mapping_profile",
]
