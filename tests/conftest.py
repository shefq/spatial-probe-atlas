from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from spatial_probe_atlas.api import mapping_dispatch_contract, system
from spatial_probe_atlas.main import create_app
from spatial_probe_atlas.settings import Settings


def _cpu_only_capability() -> SimpleNamespace:
    value = {
        "state": "cpu_only", "available": False, "device": None,
        "torch_cuda_version": None, "reason": "test fixture", "checks": [],
    }
    return SimpleNamespace(available=False, state="cpu_only", device=None, as_dict=lambda: dict(value))


@pytest.fixture
def app_settings(tmp_path):
    return Settings(
        data_root=tmp_path / "data",
        frontend_dist=None,
        allow_test_host=True,
        min_mapping_frames=3,
        disk_reserve_bytes=0,
        compute_profile="cpu",
    )


@pytest.fixture
def client(app_settings, monkeypatch):
    monkeypatch.setattr(mapping_dispatch_contract, "probe_cuda", lambda _: _cpu_only_capability())
    monkeypatch.setattr(system, "probe_cuda", lambda _: _cpu_only_capability())
    app = create_app(app_settings)
    with TestClient(app, base_url="http://testserver") as value:
        yield value
