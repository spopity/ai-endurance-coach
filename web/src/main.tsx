import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";

async function start() {
  // The demo build (npm run build:demo) answers /api in the browser; production builds drop this.
  if (import.meta.env.MODE === "demo") await import("./demo/install");
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void start();
