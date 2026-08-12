import { Navigate, Route, Routes, useLocation } from "react-router";
import { useAuth } from "./auth.tsx";
import { Shell } from "./components/Shell.tsx";
import Login from "./routes/Login.tsx";
import Mail from "./routes/Mail.tsx";
import Thread from "./routes/Thread.tsx";
import Search from "./routes/Search.tsx";
import Storage from "./routes/Storage.tsx";

/** Everything except /login needs a session. One wrapper, so no route can forget. */
function Private({ children }: { children: React.ReactNode }) {
  const { session } = useAuth();
  const loc = useLocation();
  if (!session) {
    const next = encodeURIComponent(loc.pathname + loc.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <Shell>{children}</Shell>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />

      {/* Both paths render the same screen: Mail owns the two-pane layout and reads
          `messageId` to decide whether the reading pane has anything in it. The URL
          names the message, so back, refresh and sharing all work. */}
      <Route
        path="/mail/:folder"
        element={
          <Private>
            <Mail />
          </Private>
        }
      />
      <Route
        path="/mail/:folder/:messageId"
        element={
          <Private>
            <Mail />
          </Private>
        }
      />

      <Route
        path="/threads/:threadId"
        element={
          <Private>
            <Thread />
          </Private>
        }
      />
      <Route
        path="/search"
        element={
          <Private>
            <Search />
          </Private>
        }
      />
      <Route
        path="/storage"
        element={
          <Private>
            <Storage />
          </Private>
        }
      />

      <Route path="/" element={<Navigate to="/mail/inbox" replace />} />
      <Route path="*" element={<Navigate to="/mail/inbox" replace />} />
    </Routes>
  );
}
