from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, AsyncIterator

import numpy as np

from spatial_probe_atlas.domain.errors import AppError
from spatial_probe_atlas.ports.camera import NormalizedCameraFrame


T_C_R = (1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


try:
    import cv2
except ImportError:
    cv2 = None


class Record3DAdapter:
    """Thread-safe adapter for the tested ``record3d==1.4.1`` API."""
    adapter_name = "record3d"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stream_class: Any | None = None
        self._stream: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[NormalizedCameraFrame] | None = None
        self._sequence = self._received = self._dropped = 0
        self._connected = self._stopped = False
        self._last_error: str | None = None
        self._last_pose_r: list[float] | None = None
        self._started_ns: int | None = None
        try:
            from record3d import Record3DStream  # type: ignore
            self._stream_class = Record3DStream
        except Exception as exc:
            self._last_error = str(exc)

    @property
    def available(self) -> bool:
        return self._stream_class is not None

    def _devices_raw(self) -> list[Any]:
        if not self.available:
            return []
        try:
            return list(self._stream_class.get_connected_devices())
        except Exception as exc:
            self._last_error = str(exc); return []

    def enumerate(self) -> list[dict[str, object]]:
        return [{"device_id": f"record3d:{index}", "adapter": "record3d", "name": str(getattr(device, "product_id", None) or getattr(device, "udid", None) or device), "available": True, "hardware": True, "capabilities": ["rgb", "depth", "per_frame_intrinsics"], "sdk_version": "1.4.1"} for index, device in enumerate(self._devices_raw())]

    async def connect(self, device_id: str) -> dict[str, object]:
        if not self.available:
            raise AppError("RECORD3D_SDK_UNAVAILABLE", "Record3D support is not installed on this machine.", status_code=503, suggested_action="Install record3d==1.4.1 for Python 3.11 or use Synthetic Replay.")
        try:
            index = int(device_id.split(":", 1)[1]); device = self._devices_raw()[index]
        except (ValueError, IndexError) as exc:
            raise AppError("RECORD3D_DEVICE_NOT_FOUND", "The selected Record3D device is no longer connected.", status_code=404, retryable=True) from exc
        with self._lock:
            self._loop = asyncio.get_running_loop(); self._queue = asyncio.Queue(maxsize=1)
            stream = self._stream_class()
            stream.on_new_frame = self._on_sdk_frame; stream.on_stream_stopped = self._on_stream_stopped
            self._sequence = self._received = self._dropped = 0; self._last_error = None; self._stopped = False
            try:
                connected = stream.connect(device)
            except Exception as exc:
                try:
                    stream.on_new_frame = None
                    stream.disconnect()
                except Exception:
                    pass
                self._stream = None
                raise AppError("RECORD3D_CONNECT_FAILED", "Record3D could not open the selected device.", status_code=503, retryable=True, details={"reason": str(exc)}) from exc
            if connected is False:
                try:
                    stream.on_new_frame = None
                    stream.disconnect()
                except Exception:
                    pass
                self._stream = None
                raise AppError("RECORD3D_DEVICE_BUSY", "Record3D rejected the connection; the device may be busy.", status_code=423, retryable=True)
            self._stream = stream
            self._connected = True; self._started_ns = time.monotonic_ns()
        return {"device_id": device_id, "adapter": "record3d", "sdk_version": "1.4.1", "t_c_r": list(T_C_R)}

    def _on_stream_stopped(self) -> None:
        self._stopped = True; self._connected = False

    @staticmethod
    def _resize_depth_nearest(depth: np.ndarray, height: int, width: int) -> np.ndarray:
        if depth.shape == (height, width):
            return depth
        if cv2 is not None:
            return cv2.resize(depth, (width, height), interpolation=cv2.INTER_NEAREST)
        y = np.minimum((np.arange(height) * depth.shape[0] / height).astype(int), depth.shape[0] - 1)
        x = np.minimum((np.arange(width) * depth.shape[1] / width).astype(int), depth.shape[1] - 1)
        return depth[y[:, None], x[None, :]]

    @staticmethod
    def _intrinsic_matrix(coefficients: Any) -> np.ndarray:
        if all(hasattr(coefficients, field) for field in ("fx", "fy", "tx", "ty")):
            return np.asarray([[float(coefficients.fx), 0.0, float(coefficients.tx)], [0.0, float(coefficients.fy), float(coefficients.ty)], [0.0, 0.0, 1.0]], dtype=np.float64)
        value = np.asarray(coefficients, dtype=np.float64)
        if value.size != 9:
            raise ValueError("Record3D intrinsic object has neither fx/fy/tx/ty nor nine matrix values")
        return value.reshape(3, 3)

    def _on_sdk_frame(self) -> None:
        with self._lock:
            stream, loop = self._stream, self._loop
            if stream is None or loop is None or not self._connected:
                return
            try:
                rgb_raw = stream.get_rgb_frame()
                depth_raw = stream.get_depth_frame()
                if rgb_raw is None or depth_raw is None:
                    return
                rgb = np.asarray(rgb_raw).copy()
                depth = np.asarray(depth_raw, dtype=np.float32).copy()
                k = self._intrinsic_matrix(stream.get_intrinsic_mat())
                if rgb.ndim != 3 or rgb.shape[2] < 3 or depth.ndim != 2:
                    raise ValueError("Record3D returned malformed RGB/depth shapes")
                rgb = np.ascontiguousarray(rgb[..., :3], dtype=np.uint8)
                depth = np.ascontiguousarray(self._resize_depth_nearest(depth, rgb.shape[0], rgb.shape[1]), dtype=np.float32)
                if not np.isfinite(k).all() or k[0, 0] <= 0 or k[1, 1] <= 0 or not (0 <= k[0, 2] <= rgb.shape[1] and 0 <= k[1, 2] <= rgb.shape[0]):
                    raise ValueError("Record3D returned invalid per-frame RGB intrinsics")
                if float(np.nanmedian(depth)) > 20.0:
                    depth *= 0.001
                depth[~np.isfinite(depth) | (depth <= 0)] = np.nan
                try:
                    pose = stream.get_camera_pose()
                    self._last_pose_r = [float(pose.qx), float(pose.qy), float(pose.qz), float(pose.qw), float(pose.tx), float(pose.ty), float(pose.tz)]
                except Exception:
                    self._last_pose_r = None
                timestamp = time.monotonic_ns()
                frame = NormalizedCameraFrame(self._sequence, timestamp, timestamp, int(rgb.shape[1]), int(rgb.shape[0]), tuple(float(value) for value in k.reshape(-1)), rgb.tobytes(), depth.tobytes(), "rgb8", True)
                self._sequence += 1; loop.call_soon_threadsafe(self._offer, frame)
            except Exception as exc:
                self._last_error = str(exc)

    def _offer(self, frame: NormalizedCameraFrame) -> None:
        if self._queue is None:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait(); self._dropped += 1
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(frame); self._received += 1

    async def frames(self) -> AsyncIterator[NormalizedCameraFrame]:
        if self._queue is None:
            raise RuntimeError("Record3D camera is not connected")
        while self._connected or not self._queue.empty():
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                if self._stopped:
                    break

    def health(self) -> dict[str, object]:
        elapsed = (time.monotonic_ns() - self._started_ns) / 1e9 if self._started_ns else 0.0
        return {"state": "ready" if self._connected else "stopped" if self._stopped else "not_available" if not self.available else "disconnected", "sdk_available": self.available, "sdk_version": "1.4.1" if self.available else None, "frame_count": self._received, "dropped_frames": self._dropped, "incomplete_frames": 1 if self._last_error else 0, "fps": self._received / elapsed if elapsed > 0 else 0.0, "last_error": self._last_error, "intrinsics_source": "record3d_per_frame", "depth_alignment": "rgb_aligned_nearest", "t_c_r": list(T_C_R), "record3d_pose_r_provenance": self._last_pose_r}

    async def disconnect(self) -> None:
        with self._lock:
            stream = self._stream; self._connected = False; self._stream = None
            if stream is not None:
                try:
                    stream.on_new_frame = None
                    stream.disconnect()
                except Exception as exc:
                    self._last_error = str(exc)
            self._queue = None; self._loop = None
