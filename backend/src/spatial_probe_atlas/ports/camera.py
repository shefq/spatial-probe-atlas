from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass(slots=True, frozen=True)
class NormalizedCameraFrame:
    sequence: int
    device_timestamp_ns: int
    server_timestamp_ns: int
    width: int
    height: int
    intrinsic_matrix: tuple[float, ...]
    rgb: bytes
    depth_m: bytes | tuple[float, ...] | None
    rgb_encoding: str = "rgb8"
    depth_aligned: bool = True


class CameraAdapter(Protocol):
    adapter_name: str

    def enumerate(self) -> list[dict[str, object]]: ...
    async def connect(self, device_id: str) -> dict[str, object]: ...
    def frames(self) -> AsyncIterator[NormalizedCameraFrame]: ...
    def health(self) -> dict[str, object]: ...
    async def disconnect(self) -> None: ...

