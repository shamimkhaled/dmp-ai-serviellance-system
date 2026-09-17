import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./index.css";
import { ensureAuth } from "./lib/auth";

ensureAuth().then((ready) => {
  if (!ready) return;
  createRoot(document.getElementById("root")).render(
    <StrictMode>
      <App />
    </StrictMode>
  );
}).catch((err) => {
  document.getElementById("root").innerHTML =
    `<div style="padding:2rem;color:#f8fafc;font-family:sans-serif">Sign-in failed: ${err.message}</div>`;
});
