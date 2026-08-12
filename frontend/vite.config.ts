import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The proxy is the whole reason the backend needs no CORS middleware, in dev or in
// production. The browser only ever sees one origin: Vite serves the app and forwards
// /v1 to FastAPI. Block K does the same thing with nginx in front of `dist/`.
// Adding Access-Control-Allow-Origin would widen the API's surface to buy nothing.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/v1": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
