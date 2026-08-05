import json
from pathlib import Path

from spatial_probe_atlas.api.calibration_registration import _normalize_probe
from spatial_probe_atlas.domain.validation import validate_probe_calibration


def test_sample_probe_calibration_file_normalizes_and_validates():
    sample_path = Path("tests/fixtures/sample_probe_calibration.json")
    assert sample_path.is_file()

    with sample_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    normalized = _normalize_probe(data)
    errors = validate_probe_calibration(normalized)

    assert errors == []
    assert normalized["schema_version"] == "1.0.0"
    assert normalized["units"] == "m"
    assert len(normalized["probe"]["marker_points_m"]) == 5
    assert len(normalized["probe"]["t_marker_tip"]) == 16
