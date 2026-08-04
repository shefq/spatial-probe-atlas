import { describe, expect, it } from "vitest";
import { LatestFrameBuffer, RECONNECT_DELAYS_MS, parseBinaryMessage } from "../api/streams";

describe("stream protocol", () => {
  it("parses the length-prefixed binary envelope", () => {
    const encoded = new TextEncoder().encode(JSON.stringify({ seq: 7, encoding: "jpeg" }));
    const payload = new Uint8Array([1, 2, 3, 4]);
    const buffer = new ArrayBuffer(4 + encoded.byteLength + payload.byteLength);
    const view = new DataView(buffer);
    view.setUint32(0, encoded.byteLength, true);
    new Uint8Array(buffer, 4, encoded.byteLength).set(encoded);
    new Uint8Array(buffer, 4 + encoded.byteLength).set(payload);
    const parsed = parseBinaryMessage(buffer);
    expect(parsed.header).toMatchObject({ seq: 7, encoding: "jpeg" });
    expect([...new Uint8Array(parsed.payload)]).toEqual([1, 2, 3, 4]);
  });

  it("keeps one latest frame and counts overwritten frames", () => {
    const buffer = new LatestFrameBuffer<number>();
    buffer.push(1); buffer.push(2); buffer.push(3);
    expect(buffer.take()).toBe(3);
    expect(buffer.take()).toBeUndefined();
    expect(buffer.droppedCount).toBe(2);
  });

  it("uses the specified reconnect schedule", () => {
    expect(RECONNECT_DELAYS_MS).toEqual([500, 1000, 2000, 4000, 5000]);
  });
});
