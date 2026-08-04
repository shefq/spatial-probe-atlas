import { describe, expect, it } from "vitest";
import { formatBytes, formatCoordinate, formatDuration, validateProjectName } from "../utils/format";

describe("format helpers", () => {
  it("formats local resource values", () => {
    expect(formatBytes(3 * 1024 ** 3)).toBe("3.0 GiB");
    expect(formatDuration(3661)).toBe("1:01:01");
    expect(formatCoordinate(0.01234, "mm")).toBe("12.3 mm");
  });

  it("applies the project-name contract", () => {
    expect(validateProjectName(" Phantom trial 07 ")).toBeNull();
    expect(validateProjectName("CON")).toMatch(/reserved/i);
    expect(validateProjectName("bad\u0007name")).toMatch(/control/i);
    expect(validateProjectName("x".repeat(81))).toMatch(/80/);
    expect(validateProjectName("name.")).toMatch(/end/i);
  });
});
