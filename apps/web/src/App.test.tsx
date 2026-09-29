import { render, screen } from "@testing-library/react";
import { App } from "./App";

test("renders the application name", () => {
  render(<App />);
  expect(screen.getByRole("heading", { name: "Graphene World Simulator" })).toBeTruthy();
});
