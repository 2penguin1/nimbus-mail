import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { AuthProvider } from "./auth.tsx";
import App from "./App.tsx";
import "./styles/tokens.css";
import "./styles/base.css";

// AuthProvider sits INSIDE BrowserRouter because it calls useNavigate to turn a 401
// into a redirect. Outside it, that hook would throw on the first expired token.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
);
