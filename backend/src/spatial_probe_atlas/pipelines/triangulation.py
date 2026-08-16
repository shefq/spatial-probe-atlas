import itertools
import numpy as np
import scipy.optimize

def triangulate_n_views(frames: list[dict], points_2d: list[np.ndarray]) -> np.ndarray:
    """
    Triangulate a single 3D point from N views.
    frames: list of dicts with 'K' (3x3), 'pose': {'R' (3x3), 't' (3x1)} representing T_c_w.
    points_2d: list of (x,y) pixel coordinates.
    """
    A = []
    for frame, pt in zip(frames, points_2d):
        R_cw = frame['pose']['R']
        t_cw = frame['pose']['t'].reshape(3, 1)
        K = frame['K']
        P = K @ np.hstack((R_cw, t_cw))
        x, y = pt[0], pt[1]
        A.append(x * P[2, :] - P[0, :])
        A.append(y * P[2, :] - P[1, :])
    
    A = np.asarray(A)
    _, _, Vt = np.linalg.svd(A)
    X = Vt[-1, :]
    X = X[:3] / X[3]
    return X

def get_reprojection_error(X: np.ndarray, frames: list[dict], points_2d: list[np.ndarray]) -> float:
    error = 0.0
    for frame, pt in zip(frames, points_2d):
        R_cw = frame['pose']['R']
        t_cw = frame['pose']['t'].reshape(3, 1)
        K = frame['K']
        Xc = R_cw @ X.reshape(3, 1) + t_cw
        if Xc[2, 0] <= 0:
            return float('inf')
        proj = K @ Xc
        proj = proj[:2] / proj[2]
        error += np.sum((proj.reshape(2) - np.asarray(pt))**2)
    return float(np.sqrt(error / len(points_2d)))

def match_and_triangulate_probe(frames: list[dict]) -> tuple[np.ndarray, dict]:
    """
    frames: list of dicts with 'K', 'pose' (T_c_w: 'R' and 't'), and 'keypoints' (list of 5 (x,y) points).
    Returns (5x3 array of 3D points, diagnostics)
    """
    if not frames:
        raise ValueError("No frames provided for triangulation.")
        
    ref_frame = frames[0]
    ref_pts = ref_frame['keypoints']
    
    aligned_points_2d = [[] for _ in range(5)]
    for i in range(5):
        aligned_points_2d[i].append(ref_pts[i])
        
    for i in range(1, len(frames)):
        frame = frames[i]
        pts = frame['keypoints']
        
        best_perm = None
        min_err = float('inf')
        
        for perm in itertools.permutations(range(5)):
            err = 0.0
            valid = True
            for j in range(5):
                pt_0 = ref_pts[j]
                pt_i = pts[perm[j]]
                
                X = triangulate_n_views([ref_frame, frame], [pt_0, pt_i])
                err_j = get_reprojection_error(X, [ref_frame, frame], [pt_0, pt_i])
                if err_j == float('inf'):
                    valid = False
                    break
                err += err_j
            
            if valid and err < min_err:
                min_err = err
                best_perm = perm
                
        if best_perm is None:
            raise ValueError(f"Could not find valid matching for frame {i}")
            
        for j in range(5):
            aligned_points_2d[j].append(pts[best_perm[j]])
            
    points_3d = []
    total_rms = 0.0
    
    for j in range(5):
        pts = aligned_points_2d[j]
        X = triangulate_n_views(frames, pts)
        
        def residuals(x):
            res = []
            for frame, pt in zip(frames, pts):
                R_cw = frame['pose']['R']
                t_cw = frame['pose']['t'].reshape(3, 1)
                K = frame['K']
                Xc = R_cw @ x.reshape(3, 1) + t_cw
                proj = K @ Xc
                proj = proj[:2] / proj[2]
                res.extend(proj.reshape(2) - np.asarray(pt))
            return np.array(res)
            
        opt = scipy.optimize.least_squares(residuals, X, method='lm')
        X_opt = opt.x
        points_3d.append(X_opt)
        
        err = get_reprojection_error(X_opt, frames, pts)
        total_rms += err**2
        
    points_3d = np.asarray(points_3d)
    rms = float(np.sqrt(total_rms / 5.0))
    
    diagnostics = {
        "rms_reprojection_error_px": rms
    }
    
    return points_3d, diagnostics
