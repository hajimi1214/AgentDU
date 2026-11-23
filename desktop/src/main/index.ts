import { app, BrowserWindow, dialog, ipcMain, type IpcMainInvokeEvent, type OpenDialogOptions } from "electron";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { access, appendFile, mkdir, readFile, stat, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { RuntimeHostClient } from "./host-client";
import type { EventMessage, RuntimeStatus } from "../shared/protocol";

const IPC_CHANNELS = [
  "desktop.getStatus",
  "desktop.openWorkspace",
  "desktop.startTestTask",
  "desktop.startTask",
  "desktop.cancelTask",
  "desktop.respondToPermission",
  "desktop.restartRuntime",
  "desktop.debugCrashRuntime",
] as const;

let mainWindow: BrowserWindow | null = null;
let runtimeHost: RuntimeHostClient | null = null;
let runtimeStatus: RuntimeStatus = { state: "starting" };
let activeWorkspace: string | null = null;
let isQuitting = false;
let runtimeHadUnexpectedExit = false;
let runtimeLogPath = "";
let launchMarkerPath = "";

function repositoryRoot(): string {
  return path.resolve(__dirname, "../../..");
}

function runtimeDataDirectory(): string {
  return path.join(app.getPath("userData"), "runtime");
}

function filteredRuntimeEnvironment(): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = {};
  const allowed = [
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "PATH",
    "PATHEXT",
    "COMSPEC",
  ];
  for (const name of allowed) {
    if (process.env[name]) environment[name] = process.env[name];
  }
  environment.PYTHONUTF8 = "1";
  environment.PYTHONIOENCODING = "utf-8";
  environment.AGENTDU_DESKTOP_DATA = runtimeDataDirectory();
  if (process.env.AGENTDU_CONFIG) environment.AGENTDU_CONFIG = process.env.AGENTDU_CONFIG;
  if (!app.isPackaged) environment.PYTHONPATH = path.join(repositoryRoot(), "src");
  return environment;
}

function runtimeCommand(): { executable: string; args: string[]; cwd: string } {
  if (app.isPackaged) {
    const python = path.join(process.resourcesPath, "agentdu-runtime", "python", "python.exe");
    return {
      executable: python,
      args: ["-B", "-X", "utf8", "-m", "agentdu.desktop_host", "--data-dir", runtimeDataDirectory()],
      cwd: runtimeDataDirectory(),
    };
  }
  return {
    executable: process.env.AGENTDU_DESKTOP_PYTHON || "python",
    args: ["-B", "-X", "utf8", "-m", "agentdu.desktop_host", "--data-dir", runtimeDataDirectory()],
    cwd: repositoryRoot(),
  };
}

async function appendRuntimeStderr(chunk: Buffer): Promise<void> {
  try {
    await mkdir(path.dirname(runtimeLogPath), { recursive: true });
    const logInfo = await stat(runtimeLogPath).catch(() => null);
    if (logInfo && logInfo.size + chunk.byteLength > 1_048_576) {
      const rotatedPath = `${runtimeLogPath}.1`;
      await writeFile(rotatedPath, await readFile(runtimeLogPath));
      await writeFile(runtimeLogPath, "", { flag: "w" });
    }
    const text = chunk
      .toString("utf8")
      .replace(/\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,})\b/g, "[REDACTED]")
      .replace(/(authorization\s*[:=]\s*bearer\s+)\S+/gi, "$1[REDACTED]");
    await appendFile(runtimeLogPath, text, { encoding: "utf8" });
  } catch {
    // Runtime diagnostics must not prevent the UI from observing process state.
  }
}

async function startRuntimeHost(): Promise<void> {
  const command = runtimeCommand();
  await mkdir(command.cwd, { recursive: true });
  runtimeLogPath = path.join(app.getPath("userData"), "logs", "runtime.stderr.log");
  publishStatus({ state: "starting", recoveredAfterCrash: runtimeHadUnexpectedExit });
  let child: ChildProcessWithoutNullStreams;
  try {
    child = spawn(command.executable, command.args, {
      cwd: command.cwd,
      env: filteredRuntimeEnvironment(),
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
      shell: false,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Runtime process could not start.";
    publishStatus({ state: "error", error: message, recoveredAfterCrash: runtimeHadUnexpectedExit });
    return;
  }

  const client = new RuntimeHostClient(child);
  runtimeHost = client;
  client.on("stderr", (chunk: Buffer) => void appendRuntimeStderr(chunk));
  client.on("event", (event: EventMessage) => mainWindow?.webContents.send("runtime.event", event));
  client.on("status", (status: RuntimeStatus) => {
    if (status.state === "error" && runtimeStatus.state !== "error") {
      runtimeHadUnexpectedExit = true;
    }
    publishStatus({ ...status, recoveredAfterCrash: runtimeHadUnexpectedExit });
  });

  try {
    const status = await client.initialize();
    publishStatus({ ...status, recoveredAfterCrash: runtimeHadUnexpectedExit });
  } catch {
    publishStatus({ ...client.getStatus(), recoveredAfterCrash: runtimeHadUnexpectedExit });
  }
}

function publishStatus(status: RuntimeStatus): void {
  runtimeStatus = status;
  mainWindow?.webContents.send("runtime.status", status);
}

function assertTrustedSender(event: IpcMainInvokeEvent): void {
  const frameUrl = event.senderFrame?.url;
  if (!frameUrl) throw new Error("拒绝无效的 IPC 调用来源。");
  if (app.isPackaged) {
    const expected = pathToFileURL(path.join(app.getAppPath(), "dist", "renderer", "index.html")).href;
    if (frameUrl !== expected) {
      throw new Error("拒绝来自非应用界面的 IPC 调用。");
    }
    return;
  }
  const frame = new URL(frameUrl);
  if (frame.protocol !== "http:" || frame.hostname !== "127.0.0.1" || frame.port !== "5173") {
    throw new Error("拒绝来自非开发界面的 IPC 调用。");
  }
}

function trustedInvoke(
  channel: (typeof IPC_CHANNELS)[number],
  handler: (event: IpcMainInvokeEvent, payload: unknown) => unknown | Promise<unknown>,
): void {
  ipcMain.handle(channel, (event, payload) => {
    assertTrustedSender(event);
    return handler(event, payload);
  });
}

function requireHost(): RuntimeHostClient {
  if (!runtimeHost || runtimeStatus.state !== "ready") {
    throw new Error(runtimeStatus.error ?? "Python Runtime 当前不可用。");
  }
  return runtimeHost;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function registerIpcHandlers(): void {
  for (const channel of IPC_CHANNELS) ipcMain.removeHandler(channel);

  trustedInvoke("desktop.getStatus", () => ({ ...runtimeStatus }));
  trustedInvoke("desktop.openWorkspace", async (event) => {
    const options: OpenDialogOptions = {
      title: "选择 AgentDU 工作区",
      properties: ["openDirectory", "createDirectory"],
    };
    const parentWindow = BrowserWindow.fromWebContents(event.sender);
    const result = parentWindow
      ? await dialog.showOpenDialog(parentWindow, options)
      : await dialog.showOpenDialog(options);
    if (result.canceled || !result.filePaths[0]) return null;
    const opened = await requireHost().request("workspace.open", { path: result.filePaths[0] });
    activeWorkspace = typeof opened.path === "string" ? opened.path : null;
    return activeWorkspace;
  });
  trustedInvoke("desktop.startTestTask", async () => {
    const result = await requireHost().request("test.start", {});
    if (typeof result.taskId !== "string" || typeof result.sessionId !== "string") {
      throw new Error("Runtime 没有返回有效的测试任务标识。");
    }
    return { taskId: result.taskId, sessionId: result.sessionId };
  });
  trustedInvoke("desktop.startTask", async (_event, payload) => {
    if (!isRecord(payload) || typeof payload.prompt !== "string" || payload.prompt.trim().length === 0 || payload.prompt.length > 16_000) {
      throw new Error("任务描述需为 1 至 16000 个字符。");
    }
    if (!activeWorkspace) throw new Error("请先通过系统文件选择器打开本地工作区。");
    const result = await requireHost().request("turn.start", { prompt: payload.prompt });
    if (typeof result.taskId !== "string" || typeof result.sessionId !== "string") {
      throw new Error("Runtime 没有返回有效的任务标识。");
    }
    return { taskId: result.taskId, sessionId: result.sessionId };
  });
  trustedInvoke("desktop.cancelTask", async (_event, payload) => {
    if (!isRecord(payload) || typeof payload.taskId !== "string" || payload.taskId.length > 128) {
      throw new Error("任务标识无效。");
    }
    return requireHost().request("turn.cancel", { taskId: payload.taskId });
  });
  trustedInvoke("desktop.respondToPermission", async (_event, payload) => {
    if (!isRecord(payload) || typeof payload.approvalId !== "string" || !["allow", "deny"].includes(String(payload.decision))) {
      throw new Error("审批回复无效。");
    }
    return requireHost().request("permission.respond", {
      approvalId: payload.approvalId,
      decision: payload.decision,
    });
  });
  trustedInvoke("desktop.restartRuntime", async () => {
    if (runtimeHost && runtimeStatus.state === "ready") {
      throw new Error("Runtime 正在运行。请先正常关闭应用再重启。");
    }
    runtimeHost = null;
    runtimeHadUnexpectedExit = true;
    await startRuntimeHost();
  });
  trustedInvoke("desktop.debugCrashRuntime", () => {
    if (app.isPackaged) throw new Error("该诊断操作仅在开发模式可用。");
    requireHost().terminateForDiagnostics();
    return { status: "terminated" };
  });
}

function createMainWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1200,
    height: 820,
    minWidth: 820,
    minHeight: 620,
    show: false,
    backgroundColor: "#101116",
    title: "AgentDU",
    webPreferences: {
      preload: path.join(__dirname, "../preload/index.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
  });

  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.webContents.session.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  window.once("ready-to-show", () => window.show());
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
  });

  const devServerUrl = process.env.AGENTDU_DESKTOP_DEV_SERVER_URL;
  if (!app.isPackaged && devServerUrl === "http://127.0.0.1:5173") {
    void window.loadURL(devServerUrl);
  } else {
    void window.loadFile(path.join(app.getAppPath(), "dist", "renderer", "index.html"));
  }
  return window;
}

async function shutdownRuntime(): Promise<void> {
  const client = runtimeHost;
  runtimeHost = null;
  if (client) await client.shutdown();
}

registerIpcHandlers();

app.whenReady().then(async () => {
  await mkdir(runtimeDataDirectory(), { recursive: true });
  launchMarkerPath = path.join(app.getPath("userData"), "desktop-launch.pending");
  runtimeHadUnexpectedExit = await access(launchMarkerPath).then(() => true).catch(() => false);
  await writeFile(
    launchMarkerPath,
    JSON.stringify({ startedAt: new Date().toISOString(), pid: process.pid }),
    "utf8",
  );
  mainWindow = createMainWindow();
  await startRuntimeHost();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) mainWindow = createMainWindow();
  });
});

app.on("before-quit", (event) => {
  if (isQuitting) return;
  event.preventDefault();
  void (async () => {
    await shutdownRuntime();
    if (launchMarkerPath) await unlink(launchMarkerPath).catch(() => undefined);
    isQuitting = true;
    app.quit();
  })();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
