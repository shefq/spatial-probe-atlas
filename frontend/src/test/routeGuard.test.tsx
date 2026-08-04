import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { RequirementGate } from "../app/layouts";

describe("route prerequisite explanation", () => {
  it("links to unmet workflow pages instead of hiding content", () => {
    render(<MemoryRouter><RequirementGate requirements={[{ ready: false, label: "Activate a map", route: "/projects/p/mapping" }]}><div>Later page controls</div></RequirementGate></MemoryRouter>);
    expect(screen.getByText("Activate a map →")).toHaveAttribute("href", "/projects/p/mapping");
    expect(screen.getByText("Later page controls")).toBeVisible();
  });
});
