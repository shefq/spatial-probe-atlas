# Spatial Probe Atlas

> **A high-performance augmented reality spatial registration and tissue-tracking platform.**

![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-blue)
![Architecture](https://img.shields.io/badge/Architecture-x64-lightgrey)
![Backend](https://img.shields.io/badge/Backend-Python%20%7C%20FastAPI-green)
![Frontend](https://img.shields.io/badge/Frontend-React%20%7C%20Three.js-blueviolet)

**Spatial Probe Atlas** is a Windows local-first browser application designed for precision spatial mapping, reusable five-marker probe calibration, metric board/tissue registration, live probe-tip painting, and reproducible session review and export. 

It acts as an AR localization system that bridges physical tools (such as fiber-optic sensor probes) with a digitized 3D coordinate system. 

*(Note: V1 intentionally does not include integrated spectrometers, FBG sensors, temperature logging, classification/diagnosis models, meshes/GLB, Gaussian splatting, cloud accounts, or a plugin runtime).*

---

## 🚀 Key Features

* **Real-time 3D Mapping (SfM):** Captures RGB-D data via a Record3D iPhone client and reconstructs dense 3D point-cloud reference maps.
* **Live Camera & Probe Tracking:** Utilizes highly optimized ArUco tracking, temporal pose prediction, and configurable blob detection to continuously track the camera and physical 5-marker probe at low latency.
* **Spatial Registration:** Accurately calculates the exact 3D coordinates of the probe's invisible tip relative to the point-cloud map.
* **Temporal Window Fallback:** Ensures accurate data capture by automatically searching a short temporal window of recent frames (e.g., ±0.5s) if the probe is temporarily occluded or not tracked at the exact moment of a click.
* **External Integration API:** Allows external physical devices (e.g., Python scripts controlling fiber-optic hardware) to trigger spatial point captures with custom measurements (`label`, `value`, `color`) via a REST API.
* **Hardware Accelerated:** Supports both deterministic CPU-based extraction (OpenCV SIFT) and optional CUDA-accelerated mapping (ALIKED/LightGlue with pycolmap).

---

## 🏗 Architecture Overview

Spatial Probe Atlas is implemented as a **modular monolith** with supervised local worker processes, ensuring stability and isolation for heavy computing tasks:
* **Backend:** A FastAPI Python server acting as the local nexus for WebSockets, REST endpoints, and job coordination.
* **Frontend:** A React + Vite SPA using `Zustand` for state management and pure `Three.js` for the 3D Viewer Engine.
* **Persistence:** `SQLite` handles state and metadata, while large artifacts (point clouds, images) remain inspectable files in the data root.

For an exhaustive, authoritative technical contract, refer to [ARCHITECTURE.md](ARCHITECTURE.md).

---

## ⚙️ Installation & Usage

### Prerequisites
* **OS:** 64-bit Windows 10/11
* **Hardware:** NVIDIA GPU (Recommended for CUDA profiles) or modern CPU.
* **Camera:** iPhone running the Record3D application.

### Setup and Run
The application uses repository-local `.venv` / `.runtime` dependencies, so it won't pollute your global environment. 

1. **Install Dependencies & Build Frontend:**
   ```bat
   setup.bat
   ```
   *(For unattended environments, run `setup.bat -NonInteractive -AcceptRuntimeDownloads`)*

2. **Start the Application:**
   ```bat
   run.bat
   ```
   The backend will serve the built UI and API on `http://127.0.0.1:8000`. Keep the console open while working. Use `Ctrl+C` for graceful shutdown.

### Data Storage
The default data root is `%LOCALAPPDATA%\SpatialProbeAtlas`. Project data, maps, and recordings are **never** stored inside the Git repository. 

---

## 📡 External Device API

You can trigger a spatial point capture (e.g., saving a tissue measurement) from an external script. The backend automatically correlates your API call with the active live-tracking position.

**Endpoint:** `POST /api/v1/projects/{project_id}/sessions/{session_id}/painted-points`
```json
{
  "label": "O2 Saturation",
  "value": 95.8,
  "color": "#00ffff"
}
```
*No position needs to be provided—the system reads the latest live camera and probe coordinates automatically.*

---

## 🛠 Development & Testing

We provide a suite of tools to verify application and hardware health.

**Non-destructive diagnostics:**
```bat
doctor.bat
doctor.bat -CpuMapping
```

**Development Verification (After Setup):**
```powershell
& .\scripts\verify.ps1 -CpuMapping
```

Record3D integration tests and CUDA checks are explicitly separated from deterministic CPU/replay automation. Browser E2E dependencies and commands are detailed in [tests/e2e/README.md](tests/e2e/README.md).

---

## 📚 Documentation

* [Architecture Specification](ARCHITECTURE.md)
* [User Guide](docs/USER_GUIDE.md)
* [Hardware Validation](docs/HARDWARE_VALIDATION.md)
* [Backup & Recovery](docs/BACKUP_AND_RECOVERY.md)
* [Development Guide](docs/DEVELOPMENT.md)
* [Architecture Decision Records (ADRs)](docs/adr)

---

## 🙏 Acknowledgements & External Tools

This project integrates and builds upon several exceptional open-source tools and libraries:
* **[Record3D](https://record3d.app/)**: Used for acquiring high-quality RGB-D data and device tracking from iOS devices.
* **[OpenCV](https://opencv.org/) & [ArUco](https://docs.opencv.org/4.x/d9/d6a/group__aruco.html)**: Core libraries for computer vision, camera pose estimation, and marker tracking.
* **[Three.js](https://threejs.org/)**: Powers the high-performance 3D Spatial Viewer.
* **[ALIKED](https://github.com/Shiaoming/ALIKED) & [LightGlue](https://github.com/cvg/LightGlue)**: Utilized in the CUDA mapping profile for state-of-the-art feature extraction and matching.
* **[COLMAP / pycolmap](https://colmap.github.io/)**: Provides robust Structure-from-Motion (SfM) for 3D reconstruction.

---

## ⚖️ License & Disclaimer

**License:**  
This project is licensed under the MIT License - see the `LICENSE` file for details.

**Disclaimer:**  
> [!WARNING]
> **For Research Purposes Only.**  
> Spatial Probe Atlas is an experimental platform designed strictly for research, academic, and engineering workflows. **It has not been clinically verified, validated, or approved by any regulatory body (such as the FDA, EMA, or similar) for medical, diagnostic, or surgical use.** 
> 
> The authors and contributors assume **no responsibility or liability** for any errors, inaccuracies, or outcomes resulting from the use of this software. Any deployment in sensitive or life-critical environments is done entirely at your own risk.

---
*Developed for advanced AR optical tracking and research workflows.*
