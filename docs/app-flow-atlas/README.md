# Spatial Probe Atlas application flow atlas

This folder contains the detailed LaTeX/TikZ flow atlas for the current v1 application.

- `spatial-probe-atlas-flow.tex` is the editable source.
- `spatial-probe-atlas-flow.pdf` is the compiled deliverable.

The atlas is intentionally multi-page. It covers the system boundary, UI route flow, camera and capture, all mapping profiles, probe calibration and detector tuning, both registration modes, live tracking and painting, manual annotation, review and exports, jobs and recovery, operations/security, persistence, coordinate transforms, API families, and current release gaps.

Compile from the repository root with the Codex LaTeX helper or any modern LaTeX installation:

```powershell
python C:\Users\mshef\.codex\plugins\cache\openai-bundled\latex\0.2.4\scripts\compile_latex.py D:\CVAI\spatial-probe-atlas\docs\app-flow-atlas\spatial-probe-atlas-flow.tex --compiler texlive
```

