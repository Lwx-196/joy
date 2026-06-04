import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Ico, IssueCountBadge } from "./atoms";

describe("atoms", () => {
  it("renders icon and issue badge smoke path", () => {
    render(
      <div>
        <Ico name="alert" size={18} className="test-alert-icon" />
        <IssueCountBadge count={3} />
      </div>
    );

    expect(document.querySelector(".test-alert-icon")).toHaveAttribute("viewBox", "0 0 24 24");
    expect(screen.getByText("3")).toBeInTheDocument();
  });
});
