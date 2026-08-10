import { useEffect, useRef, useState, type MouseEvent } from "react";
import { api, errorMessage } from "../api/client";
import { Button, Modal, InlineAlert } from "./ui";

export function ManualAnnotationModal({
  open,
  projectId,
  sessionId,
  record,
  onClose,
  onSuccess,
}: {
  open: boolean;
  projectId: string;
  sessionId: string;
  record: any;
  onClose: () => void;
  onSuccess: (updatedRecord: any) => void;
}) {
  const [points, setPoints] = useState<[number, number][]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);

  useEffect(() => {
    if (!open || !record) {
      setPoints([]);
      setError(null);
      return;
    }
    const img = new Image();
    img.src = `/api/v1/projects/${projectId}/sessions/${sessionId}/painted-records/${record.id}/image`;
    img.onload = () => {
      imgRef.current = img;
      draw();
    };
  }, [open, record, projectId, sessionId]);

  const draw = () => {
    const canvas = canvasRef.current;
    if (!canvas || !imgRef.current) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    
    canvas.width = imgRef.current.width;
    canvas.height = imgRef.current.height;
    
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(imgRef.current, 0, 0);
    
    points.forEach((p, i) => {
      ctx.beginPath();
      ctx.arc(p[0], p[1], 4, 0, 2 * Math.PI);
      ctx.fillStyle = i === 4 ? "#10b981" : "#f59e0b";
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1.5;
      ctx.stroke();
      
      ctx.fillStyle = "#fff";
      ctx.font = "12px Inter, sans-serif";
      ctx.fillText((i + 1).toString(), p[0] + 8, p[1] + 4);
    });
  };

  useEffect(() => {
    draw();
  }, [points]);

  const handleCanvasClick = (e: MouseEvent<HTMLCanvasElement>) => {
    if (points.length >= 5) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    
    const x = (e.clientX - rect.left) * scaleX;
    const y = (e.clientY - rect.top) * scaleY;
    
    setPoints([...points, [x, y]]);
  };

  const submit = async () => {
    if (points.length !== 5) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.sessions.annotate(projectId, sessionId, record.id, points);
      onSuccess(res);
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title="Manual Probe Annotation"
      description="Click the 5 fiducial markers on the probe in order."
      onRequestClose={onClose}
      size="lg"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button disabled={points.length === 0} onClick={() => setPoints(points.slice(0, -1))}>Undo Last Point</Button>
          <Button variant="primary" busy={busy} disabled={points.length !== 5} onClick={() => void submit()}>
            Submit Annotation
          </Button>
        </>
      }
    >
      {error && <InlineAlert tone="danger" title="Annotation Failed">{error}</InlineAlert>}
      <div style={{ width: "100%", overflow: "auto", background: "#111", borderRadius: "8px", border: "1px solid #333", marginTop: "12px", textAlign: "center" }}>
        <canvas
          ref={canvasRef}
          onClick={handleCanvasClick}
          style={{ maxWidth: "100%", height: "auto", cursor: points.length < 5 ? "crosshair" : "default" }}
        />
      </div>
      <div style={{ marginTop: "12px", fontSize: "13px", color: "#888" }}>
        Selected: {points.length} / 5 points
      </div>
    </Modal>
  );
}
