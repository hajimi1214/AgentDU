import { createServer } from "node:net";
import { request } from "node:http";
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const viteEntry = path.join(path.resolve(path.dirname(require.resolve("vite")), "../.."), "bin", "vite.js");
const electronExecutable = require("electron");

const port = await findAvailablePort();
const devServerUrl = `http://127.0.0.1:${port}`;
const vite = spawn(
  process.execPath,
  [viteEntry, "--host", "127.0.0.1", "--port", String(port), "--strictPort"],
  { cwd: desktopRoot, stdio: "inherit", windowsHide: true },
);

let electron;
let shutdownRequested = false;
const stopChildren = () => {
  shutdownRequested = true;
  if (electron && electron.exitCode === null) electron.kill();
  if (vite.exitCode === null) vite.kill();
};

process.once("SIGINT", stopChildren);
process.once("SIGTERM", stopChildren);

try {
  await waitForServer(vite, devServerUrl);
  electron = spawn(electronExecutable, ["."], {
    cwd: desktopRoot,
    env: { ...process.env, AGENTDU_DESKTOP_DEV_SERVER_URL: devServerUrl },
    stdio: "inherit",
    windowsHide: false,
  });

  const exitCode = await Promise.race([
    new Promise((resolve, reject) => {
      electron.once("error", reject);
      electron.once("exit", (code) => resolve(code ?? 1));
    }),
    new Promise((resolve) => {
      vite.once("exit", (code) => resolve(code ?? 1));
    }),
  ]);
  if (!shutdownRequested) process.exitCode = exitCode;
} finally {
  stopChildren();
}

async function findAvailablePort() {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    server.close();
    throw new Error("Could not reserve a local development port.");
  }
  await new Promise((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
  });
  return address.port;
}

async function waitForServer(child, url) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Vite exited before becoming ready with code ${child.exitCode}.`);
    }
    const ready = await probeServer(url);
    if (ready) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Vite did not become ready at ${url}.`);
}

function probeServer(url) {
  return new Promise((resolve) => {
    const probe = request(url, { method: "GET" }, (response) => {
      response.resume();
      resolve(response.statusCode !== undefined);
    });
    probe.setTimeout(500, () => {
      probe.destroy();
      resolve(false);
    });
    probe.once("error", () => resolve(false));
    probe.end();
  });
}
