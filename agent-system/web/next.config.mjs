import { fileURLToPath } from "node:url";

/** @type {import('next').NextConfig} */
const nextConfig = {
  // The repo lives at "<home>/Downloads/Bob Agent/agent-system/web" while a
  // stray package-lock.json sits in <home>; without pinning the tracing root,
  // Turbopack warns and file tracing walks outside the project.
  turbopack: {
    root: fileURLToPath(new URL(".", import.meta.url)),
  },
  outputFileTracingRoot: fileURLToPath(new URL(".", import.meta.url)),
  allowedDevOrigins: [
    "localhost",
    "127.0.0.1",
    "192.168.29.96",
    "0.0.0.0",
  ],
};

export default nextConfig;
