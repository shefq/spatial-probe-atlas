from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    name: str


class ProjectUpdate(BaseModel):
    name: str | None = None


class CloneRequest(BaseModel):
    name: str | None = None


class CameraConnectRequest(BaseModel):
    project_id: str
    adapter: Literal["record3d", "replay"]
    device_id: str
    owner: str = "camera_setup"


class CaptureSetCreate(BaseModel):
    name: str = "Capture set"
    source: Literal["record3d", "replay", "import"] = "record3d"


class CaptureFramesRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=500)
    interval_ms: int = Field(default=0, ge=0, le=60000)


class FrameImportItem(BaseModel):
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    intrinsic_matrix: list[float] = Field(min_length=9, max_length=9)
    rgb_base64: str
    depth_f32_base64: str
    timestamp_ns: int | None = None


class FrameImportRequest(BaseModel):
    frames: list[FrameImportItem] = Field(min_length=1, max_length=500)


class FrameUpdate(BaseModel):
    included: bool
    exclusion_reason: str | None = Field(default=None, max_length=500)


class MapCreate(BaseModel):
    capture_set_id: str
    capture_set_revision: int | None = None
    compute_profile: Literal["auto", "cpu", "cuda"] = "auto"
    name: str = Field(default="Reference map", min_length=1, max_length=120)


class MapTransformUpdate(BaseModel):
    position: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    quaternion: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0, 1.0])
    scale: float = 1.0


class ValidationImportRequest(BaseModel):
    validation_id: str
    activate: bool = True


class CalibrationRevisionRequest(BaseModel):
    blob_detector: dict[str, Any]
    name: str | None = None
    activate: bool = True


class RegistrationCreate(BaseModel):
    name: str = Field(default="Map registration", min_length=1, max_length=120)
    map_id: str | None = None
    probe_calibration_id: str | None = None
    board_definition: dict[str, Any] | None = None


class RegistrationObservation(BaseModel):
    source_point_m0: list[float] = Field(min_length=3, max_length=3)
    target_point_w: list[float] = Field(min_length=3, max_length=3)
    label: str | None = Field(default=None, max_length=120)


class RegistrationValidationRequest(BaseModel):
    accept_warning: bool = False
    note: str | None = Field(default=None, max_length=1000)


class SessionCreate(BaseModel):
    name: str = Field(default="Live session", min_length=1, max_length=120)
    notes: str = Field(default="", max_length=4000)
    compute_profile: Literal["auto", "cpu", "cuda"] = "auto"


class SessionNote(BaseModel):
    notes: str = Field(max_length=4000)


class PaintedPointCreate(BaseModel):
    command_id: str | None = None
    frame_id: int | None = None
    position_w_m: list[float] | None = Field(default=None, min_length=3, max_length=3)
    quality: str | None = None
    note: str = Field(default="", max_length=1000)
    label: str | None = Field(default=None, max_length=120)
    value: float | None = None
    color: str | None = Field(default=None, max_length=20)
    low_quality_override_reason: str | None = Field(default=None, max_length=500)
    save_image: bool = False
    image_uri: str | None = None


class RecordAnnotationCreate(BaseModel):
    points_px: list[list[float]] = Field(..., min_length=5, max_length=5)



class PaintedPathCreate(BaseModel):
    command_id: str
    samples: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    sampling_policy: dict[str, Any] = Field(default_factory=lambda: {"mode": "time", "interval_ms": 100})
    note: str = Field(default="", max_length=1000)


class RecordPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=1000)


class ExportCreate(BaseModel):
    format: Literal["json", "csv", "session_manifest", "screenshot", "point_overlay"] = "json"
    filters: dict[str, Any] = Field(default_factory=dict)
    include_deleted: bool | None = None


class SettingsPatch(BaseModel):
    display_units: Literal["m", "mm"] | None = None
    compute_profile: Literal["auto", "cpu", "cuda"] | None = None
    point_budget: int | None = Field(default=None, ge=500000, le=10000000)
    decoded_cache_mib: int | None = Field(default=None, ge=128, le=4096)
