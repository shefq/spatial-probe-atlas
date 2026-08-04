import "@testing-library/jest-dom/vitest";

class TestResizeObserver implements ResizeObserver {
  readonly root = null;
  readonly rootMargin = "0px";
  readonly thresholds = [0];
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords(): ResizeObserverEntry[] { return []; }
}

globalThis.ResizeObserver = TestResizeObserver;
globalThis.requestAnimationFrame = (callback: FrameRequestCallback) => window.setTimeout(() => callback(performance.now()), 0);
globalThis.cancelAnimationFrame = (id: number) => window.clearTimeout(id);
Object.defineProperty(URL, "createObjectURL", { configurable: true, value: () => "blob:test" });
Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => undefined });
