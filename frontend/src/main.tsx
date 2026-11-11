import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/layout.css";
import "./styles/transcript.css";
import "./styles/composer.css";
import "./styles/filepanel.css";
import "./styles/overlays.css";
import "./styles/starmap.css";
import "./styles/hljs-theme.css";
import App from "./App";
import { LIVE } from "./mode";
import { startLive } from "./live";

// Live or mock is decided before anything is drawn: inside WittyCode the store starts empty (store.ts) and talks to
// Python; in the design review it holds the demo data.
if (LIVE) startLive();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
