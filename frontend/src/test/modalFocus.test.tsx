import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useState } from "react";
import { Modal, TextInput } from "../components/ui";

function TestModal() {
  const [value, setValue] = useState("");
  const onRequestClose = () => undefined;
  return (
    <Modal open title="Create project" onRequestClose={onRequestClose}>
      <label>
        Project name
        <TextInput aria-label="Project name" value={value} onChange={(event) => setValue(event.target.value)} />
      </label>
    </Modal>
  );
}

describe("Modal", () => {
  it("keeps focus on a controlled input while typing", () => {
    render(<TestModal />);

    const input = screen.getByLabelText("Project name");
    input.focus();
    fireEvent.change(input, { target: { value: "t" } });

    expect(input).toHaveFocus();
  });
});