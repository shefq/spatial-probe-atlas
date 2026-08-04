import type { TrackingViewFrame, WsEnvelope } from "./types";

export const RECONNECT_DELAYS_MS = [500, 1000, 2000, 4000, 5000] as const;

export interface BinaryStreamMessage {
  header: Record<string, unknown>;
  payload: ArrayBuffer;
}

export function parseBinaryMessage(buffer: ArrayBuffer): BinaryStreamMessage {
  if (buffer.byteLength < 4) throw new Error("Binary stream message is too short.");
  const view = new DataView(buffer);
  const headerLength = view.getUint32(0, true);
  if (headerLength > buffer.byteLength - 4) throw new Error("Binary stream header length is invalid.");
  const headerBytes = new Uint8Array(buffer, 4, headerLength);
  const header = JSON.parse(new TextDecoder().decode(headerBytes)) as Record<string, unknown>;
  return { header, payload: buffer.slice(4 + headerLength) };
}

function wsUrl(path: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${path}`;
}

export interface ReconnectingSocketOptions {
  onEnvelope?: (envelope: WsEnvelope) => void;
  onBinary?: (message: BinaryStreamMessage) => void;
  onState?: (state: "connecting" | "open" | "closed" | "reconnecting") => void;
  onOpen?: (reconnected: boolean) => void;
  onSequenceGap?: (expected: number, received: number) => void;
  onError?: (message: string) => void;
}

export class ReconnectingSocket {
  private socket: WebSocket | null = null;
  private stopped = false;
  private attempt = 0;
  private reconnectTimer: number | null = null;
  private lastSequence: number | null = null;
  private readonly queue: string[] = [];
  private readonly sticky = new Map<string, string>();

  constructor(
    private readonly path: string,
    private readonly options: ReconnectingSocketOptions = {},
  ) {}

  connect(): void {
    this.stopped = false;
    this.open();
  }

  private open(): void {
    if (this.stopped || this.socket?.readyState === WebSocket.OPEN || this.socket?.readyState === WebSocket.CONNECTING) return;
    this.options.onState?.(this.attempt ? "reconnecting" : "connecting");
    const socket = new WebSocket(wsUrl(this.path));
    socket.binaryType = "arraybuffer";
    this.socket = socket;
    socket.addEventListener("open", () => {
      const reconnected = this.attempt > 0;
      this.attempt = 0;
      this.lastSequence = null;
      this.options.onState?.("open");
      this.sticky.forEach((payload) => socket.send(payload));
      while (this.queue.length) socket.send(this.queue.shift()!);
      this.options.onOpen?.(reconnected);
    });
    socket.addEventListener("message", (event) => {
      try {
        if (event.data instanceof ArrayBuffer) {
          this.options.onBinary?.(parseBinaryMessage(event.data));
          return;
        }
        const envelope = JSON.parse(String(event.data)) as WsEnvelope;
        if (typeof envelope.seq === "number") {
          if (this.lastSequence !== null && envelope.seq > this.lastSequence + 1) {
            this.options.onSequenceGap?.(this.lastSequence + 1, envelope.seq);
          }
          if (this.lastSequence === null || envelope.seq > this.lastSequence) this.lastSequence = envelope.seq;
        }
        this.options.onEnvelope?.(envelope);
      } catch (error) {
        this.options.onError?.(error instanceof Error ? error.message : "Invalid stream message.");
      }
    });
    socket.addEventListener("close", () => {
      if (this.socket === socket) this.socket = null;
      if (this.stopped) {
        this.options.onState?.("closed");
        return;
      }
      this.scheduleReconnect();
    });
    socket.addEventListener("error", () => this.options.onError?.("Stream connection failed."));
  }

  private scheduleReconnect(): void {
    this.options.onState?.("reconnecting");
    const base = RECONNECT_DELAYS_MS[Math.min(this.attempt, RECONNECT_DELAYS_MS.length - 1)];
    this.attempt += 1;
    const jitter = base * (Math.random() * 0.2 - 0.1);
    this.reconnectTimer = window.setTimeout(() => this.open(), Math.round(base + jitter));
  }

  send(type: string, data: Record<string, unknown> = {}): string {
    const commandId = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
    const payload = JSON.stringify({ protocol_version: 1, type, command_id: commandId, data });
    if (["subscribe", "set_preview", "tuning.patch"].includes(type)) {
      this.sticky.set(type, payload);
      if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(payload);
      return commandId;
    }
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(payload);
    else this.queue.push(payload);
    return commandId;
  }

  close(): void {
    this.stopped = true;
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.queue.length = 0;
    this.sticky.clear();
    this.socket?.close(1000, "client closing");
    this.socket = null;
    this.options.onState?.("closed");
  }
}

export class LatestFrameBuffer<T> {
  private latest: T | undefined;
  private dropped = 0;

  push(value: T): void {
    if (this.latest !== undefined) this.dropped += 1;
    this.latest = value;
  }

  take(): T | undefined {
    const value = this.latest;
    this.latest = undefined;
    return value;
  }

  get droppedCount(): number {
    return this.dropped;
  }
}

export function createTrackingStream(
  projectId: string,
  sessionId: string,
  onFrame: (frame: TrackingViewFrame) => void,
  options: Omit<ReconnectingSocketOptions, "onEnvelope"> = {},
): ReconnectingSocket {
  return new ReconnectingSocket(`/ws/v1/projects/${projectId}/sessions/${sessionId}/tracking`, {
    ...options,
    onEnvelope: (envelope) => {
      if (envelope.type === "tracking.frame") onFrame(envelope.data as TrackingViewFrame);
    },
  });
}
