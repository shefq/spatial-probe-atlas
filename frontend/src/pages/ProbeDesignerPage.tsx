import { Link } from "react-router-dom";
import { ProbeDesignerStudio } from "../features/probe/ProbeDesignerStudio";

export function ProbeDesignerPage() {
  return (
    <main className="page page--probe-designer">
      <header className="page-heading page-heading--wide">
        <div className="page-heading__actions">
          <Link to="/projects" className="back-link">← Projects</Link>
        </div>
        <div>
          <div className="eyebrow">CAD & CALIBRATION STUDIO</div>
          <h1>Probe Designer & Blender Generator</h1>
          <p>
            Adjust 5-marker constellation coordinates, shaft offsets, sleeve geometry, and arm tapers. Preview in real-time 3D, then copy or export the generated Blender Python script to create 3D-printable STLs.
          </p>
        </div>
      </header>
      <ProbeDesignerStudio />
    </main>
  );
}
