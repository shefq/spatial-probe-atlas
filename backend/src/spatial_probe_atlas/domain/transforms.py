from __future__ import annotations

from typing import Any

import numpy as np

from .errors import AppError


def solve_similarity(source: list[list[float]], target: list[list[float]]) -> dict[str, Any]:
    """Umeyama least-squares similarity mapping M0 points into W."""
    x = np.asarray(source, dtype=float)
    y = np.asarray(target, dtype=float)
    if x.shape != y.shape or x.ndim != 2 or x.shape[1] != 3 or x.shape[0] < 3:
        raise AppError("REGISTRATION_OBSERVATIONS_INVALID", "At least three paired 3D observations are required.", status_code=422)
    if not np.isfinite(x).all() or not np.isfinite(y).all() or np.linalg.matrix_rank(x - x.mean(0)) < 2:
        raise AppError("REGISTRATION_GEOMETRY_DEGENERATE", "Registration observations are degenerate.", status_code=422)
    mx, my = x.mean(0), y.mean(0)
    xc, yc = x - mx, y - my
    covariance = (yc.T @ xc) / len(x)
    u, singular, vt = np.linalg.svd(covariance)
    sign = np.ones(3)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        sign[-1] = -1
    rotation = u @ np.diag(sign) @ vt
    variance = float(np.mean(np.sum(xc * xc, axis=1)))
    scale = float((singular * sign).sum() / variance)
    if not math_is_positive(scale):
        raise AppError("REGISTRATION_SCALE_INVALID", "The solved scale is not finite and positive.", status_code=422)
    translation = my - scale * (rotation @ mx)
    predicted = (scale * (rotation @ x.T)).T + translation
    residuals = np.linalg.norm(predicted - y, axis=1)
    return {
        "source_frame": "M0", "destination_frame": "W", "units": "m", "convention_version": 1,
        "scale": scale, "rotation": rotation.reshape(-1).tolist(), "translation": translation.tolist(),
        "rms_residual_m": float(np.sqrt(np.mean(residuals**2))), "max_residual_m": float(residuals.max()),
        "residuals_m": residuals.tolist(), "observation_count": len(x),
    }


def math_is_positive(value: float) -> bool:
    return bool(np.isfinite(value) and value > 0)


def compose_tip(t_w_c: list[float], t_c_m: list[float], t_m_p: list[float]) -> list[float]:
    matrices = [np.asarray(value, dtype=float).reshape(4, 4) for value in (t_w_c, t_c_m, t_m_p)]
    result = matrices[0] @ matrices[1] @ matrices[2]
    if not np.isfinite(result).all():
        raise AppError("TRACKING_TRANSFORM_INVALID", "Tracking produced a non-finite transform.", status_code=422)
    return result.reshape(-1).tolist()

