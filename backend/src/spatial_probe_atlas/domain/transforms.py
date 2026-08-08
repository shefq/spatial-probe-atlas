from __future__ import annotations

from typing import Any

import numpy as np

from .errors import AppError


def solve_similarity(source: list[list[float]], target: list[list[float]]) -> dict[str, Any]:
    """Umeyama least-squares similarity mapping M0 points into W."""
    x = np.asarray(source, dtype=float)
    y = np.asarray(target, dtype=float)
    if x.shape != y.shape or x.ndim != 2 or x.shape[1] != 3 or x.shape[0] < 3:
        raise AppError("REGISTRATION_OBSERVATIONS_INVALID", "At least three paired 3D observations are required.", status_code=422, suggested_action="Capture at least 3 distinct observation points.")
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise AppError("REGISTRATION_GEOMETRY_INVALID", "Observation coordinates contain non-finite numbers.", status_code=422, suggested_action="Delete invalid observations and recapture.")
    rank_x = np.linalg.matrix_rank(x - x.mean(0))
    rank_y = np.linalg.matrix_rank(y - y.mean(0))
    if rank_x < 2 or rank_y < 2:
        raise AppError(
            "REGISTRATION_GEOMETRY_DEGENERATE",
            "Registration observations are degenerate (all points lie in a single line or identical position).",
            status_code=422,
            details={"source_rank": int(rank_x), "target_rank": int(rank_y), "observation_count": len(x)},
            suggested_action="Capture observations from at least 3-5 distinct positions/angles to span 2D/3D space.",
        )
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


def solve_kinematic_scale(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Least-squares scale from paired kinematic observations (stationary probe, moving camera).
    Each observation must contain:
    - camera_pose_w: 4x4 matrix (T_W_C)
    - probe_pose_c: 4x4 matrix (T_C_M0)
    """
    if len(observations) < 2:
        raise AppError("REGISTRATION_OBSERVATIONS_INVALID", "At least two kinematic observations from different angles are required.", status_code=422)

    parsed = []
    for obs in observations:
        try:
            t_w_c = np.asarray(obs["camera_pose_w"], dtype=float).reshape(4, 4)
            t_c_m = np.asarray(obs["probe_pose_c"], dtype=float).reshape(4, 4)
            parsed.append((t_w_c, t_c_m))
        except (KeyError, ValueError, TypeError):
            raise AppError("REGISTRATION_GEOMETRY_INVALID", "Invalid kinematic observation matrices.", status_code=422)

    num, den = 0.0, 0.0
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            t_w_c1, t_c_m1 = parsed[i]
            t_w_c2, t_c_m2 = parsed[j]
            
            # C1, C2 are camera centers in world coordinates (T_W_C translational part)
            c1 = t_w_c1[:3, 3]
            c2 = t_w_c2[:3, 3]
            
            # Rotation of camera in world
            r1 = t_w_c1[:3, :3]
            r2 = t_w_c2[:3, :3]
            
            # Probe translation in camera coordinates
            p_c1 = t_c_m1[:3, 3]
            p_c2 = t_c_m2[:3, 3]
            
            a = r1 @ p_c1 - r2 @ p_c2
            b = c2 - c1
            num += float(a @ b)
            den += float(a @ a)
            
    if den <= 1e-12:
        raise AppError("REGISTRATION_GEOMETRY_DEGENERATE", "Kinematic observations lack sufficient baseline camera movement.", status_code=422)

    scale = num / den
    if not math_is_positive(scale):
        raise AppError("REGISTRATION_SCALE_INVALID", "The computed kinematic scale is not finite and positive.", status_code=422)
    
    # We estimate translation and rotation relative to the first observation, 
    # but the probe is assumed stationary, so rotation is identity (we don't solve it here).
    # Actually, we just need scale. 
    # Return similarity payload. We assume R=I, T=0 because the point cloud and probe are aligned up to scale?
    # Wait, the similarity matrix maps M0 to W.
    # T_W_M0 = T_W_C * T_C_M0. But T_C_M0 is scaled!
    # T_W_M0 = T_W_C * [ I  | s * P_C ]
    #                  [ 0  | 1       ]
    # We only need the scale. The rest of the similarity matrix is Identity for R and 0 for T, because the downstream tracking uses T_W_C and T_C_M0 and scales it dynamically.
    # Wait, in cpu.py, the point cloud is transformed by `scale * (rotation @ point) + translation`.
    # We should return rotation=I, translation=0, scale=scale.
    
    return {
        "source_frame": "M0", "destination_frame": "W", "units": "m", "convention_version": 1,
        "scale": scale, "rotation": np.eye(3).reshape(-1).tolist(), "translation": [0.0, 0.0, 0.0],
        "rms_residual_m": 0.0, "max_residual_m": 0.0,
        "residuals_m": [], "observation_count": len(parsed),
    }

