import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { AuthProvider } from "./auth.tsx";
import { ErrorBoundary } from "./components/ErrorBoundary.tsx";
import App from "./App.tsx";
import "./styles/tokens.css";
import "./styles/base.css";

// AuthProvider sits INSIDE BrowserRouter because it calls useNavigate to turn a 401
// into a redirect. Outside it, that hook would throw on the first expired token.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* The outer net. The one inside Shell (App.tsx) keeps the chrome alive when a
        route throws; this one catches what that cannot — Shell itself, AuthProvider,
        and /login, which is outside Private entirely. Two boundaries because they
        cover different trees, not for depth. */}
    <ErrorBoundary>
      <BrowserRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </ErrorBoundary>
  </StrictMode>,
);
