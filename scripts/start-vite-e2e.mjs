import { fileURLToPath } from "node:url";
import path from "node:path";

import react from "../frontend/node_modules/@vitejs/plugin-react/dist/index.js";
import { createServer } from "../frontend/node_modules/vite/dist/node/index.js";

const host = process.env.TRIP_E2E_VITE_HOST || "127.0.0.1";
const port = Number(process.env.TRIP_E2E_VITE_PORT || 0);
if (!Number.isInteger(port) || port <= 0 || port > 65535) {
  throw new Error("TRIP_E2E_VITE_PORT must be a valid TCP port");
}

const frontendRoot = fileURLToPath(new URL("../frontend/", import.meta.url));
const cacheDir = process.env.TRIP_E2E_VITE_CACHE_DIR
  ? path.resolve(process.env.TRIP_E2E_VITE_CACHE_DIR)
  : path.join(frontendRoot, "node_modules", ".vite-e2e");

// Loading the TypeScript Vite config makes Vite emit an executable timestamp
// module beside the tracked config. Some Windows sandbox/endpoint policies
// reject that transient file with EPERM. The live gate only needs the React
// transform and an isolated server, so build its config in memory and keep all
// disposable optimizer output under the run directory.
const server = await createServer({
  root: frontendRoot,
  configFile: false,
  cacheDir,
  plugins: [react()],
  server: {
    host,
    port,
    strictPort: true,
  },
});

await server.listen();
server.printUrls();
