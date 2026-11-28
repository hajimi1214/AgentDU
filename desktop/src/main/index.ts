import { app, BrowserWindow, dialog, ipcMain, safeStorage, type IpcMainInvokeEvent, type OpenDialogOptions } from "electron";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { access, appendFile, mkdir, readFile, rename, stat, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { RuntimeHostClient } from "./host-client";
import type { EventMessage, ProviderTestFailureKind, ProviderTestResult, RuntimeStatus } from "../shared/protocol";

// Packaged acceptance runs can isolate app data without borrowing the user's profile.
const isolatedUserDataDirectory = process.env.AGENTDU_DESKTOP_USER_DATA_DIR?.trim();
if (isolatedUserDataDirectory) {
  app.setPath("userData", path.resolve(isolatedUserDataDirectory));
}

const IPC_CHANNELS = [
  "desktop.getStatus",
  "desktop.openWorkspace",
  "desktop.openRecentWorkspace",
  "desktop.listWorkspaces",
  "desktop.inspectWorkspace",
  "desktop.listFiles",
  "desktop.readFile",
  "desktop.writeFile",
  "desktop.getGitStatus",
  "desktop.getGitDiff",
  "desktop.saveProviderProfile",
  "desktop.deleteProviderProfile",
  "desktop.testProvider",
  "desktop.listSessions",
  "desktop.createSession",
  "desktop.readSession",
  "desktop.runCommand",
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
const interruptedTaskCandidates = new Map<string, {
  sessionId: string;
  workspacePath: string | null;
}>();
let isQuitting = false;
let runtimeHadUnexpectedExit = false;
let runtimeLogPath = "";
let launchMarkerPath = "";
let providerStoreInitialization: Promise<void> | null = null;
let providerUpdateInProgress = false;

type StoredProviderProfile = {
  name: string;
  baseUrl: string;
  model: string;
  encryptedApiKey?: string;
  protocol?: string;
  wireApi?: "chat_completions" | "responses";
  streaming?: boolean;
  configuredCapabilities?: string[];
};
type ProviderStore = { defaultProfile: string | null; profiles: StoredProviderProfile[] };

function repositoryRoot(): string {
  return path.resolve(__dirname, "../../..");
}

function runtimeDataDirectory(): string {
  return path.join(app.getPath("userData"), "runtime");
}

function providerStorePath(): string {
  return path.join(app.getPath("userData"), "providers.secure.json");
}

async function readProviderStore(): Promise<ProviderStore> {
  await initializeProviderStore();
  let source: string;
  try {
    source = await readFile(providerStorePath(), "utf8");
  } catch (error) {
    if (isRecord(error) && error.code === "ENOENT") return { defaultProfile: null, profiles: [] };
    throw new Error(`无法读取已保存的 Provider 档案：${error instanceof Error ? error.message : String(error)}`);
  }
  let parsed: unknown;
  try { parsed = JSON.parse(source); } catch { throw new Error("Provider 档案文件损坏，已停止加载以保护已有配置。"); }
  if (!isRecord(parsed) || !Array.isArray(parsed.profiles)) throw new Error("Provider 档案文件格式无效，已停止加载以保护已有配置。");
  const profiles = parsed.profiles.filter((item): item is StoredProviderProfile =>
    isRecord(item) && typeof item.name === "string" && typeof item.baseUrl === "string" && typeof item.model === "string" &&
    (item.encryptedApiKey === undefined || typeof item.encryptedApiKey === "string"),
  );
  return { defaultProfile: typeof parsed.defaultProfile === "string" ? parsed.defaultProfile : null, profiles };
}

async function initializeProviderStore(): Promise<void> {
  if (!providerStoreInitialization) {
    providerStoreInitialization = (async () => {
      try {
        await access(providerStorePath());
        return;
      } catch (error) {
        if (isRecord(error) && error.code !== "ENOENT") throw error;
      }
      const migrated = await migrateLegacyProviderStore();
      if (migrated) await writeProviderStore(migrated);
    })();
  }
  try {
    await providerStoreInitialization;
  } catch (error) {
    providerStoreInitialization = null;
    throw error;
  }
}

async function writeProviderStore(store: ProviderStore): Promise<void> {
  const target = providerStorePath();
  await mkdir(path.dirname(target), { recursive: true });
  const temporary = `${target}.tmp`;
  await writeFile(temporary, JSON.stringify(store), { encoding: "utf8", mode: 0o600 });
  await rename(temporary, target);
}

function providerEnvironmentName(name: string): string {
  return `AGENTDU_DESKTOP_PROFILE_${Buffer.from(name, "utf8").toString("hex").toUpperCase()}_KEY`;
}

function tomlString(value: string): string {
  return JSON.stringify(value);
}

function readTomlString(source: string, key: string): string | null {
  const escapedKey = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^\\s*${escapedKey}\\s*=\\s*("(?:\\\\.|[^"])*"|'[^']*')\\s*$`, "m").exec(source);
  if (!match) return null;
  if (match[1].startsWith('"')) {
    try { return JSON.parse(match[1]) as string; } catch { return null; }
  }
  return match[1].slice(1, -1);
}

function readTomlBoolean(source: string, key: string): boolean | null {
  const match = new RegExp(`^\\s*${key.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&")}\\s*=\\s*(true|false)\\s*$`, "m").exec(source);
  return match ? match[1] === "true" : null;
}

function readTomlStringArray(source: string, key: string): string[] {
  const escapedKey = key.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&");
  const match = new RegExp(`^\\s*${escapedKey}\\s*=\\s*(\\[[^\\]]*\\])\\s*$`, "m").exec(source);
  if (!match) return [];
  try {
    const parsed: unknown = JSON.parse(match[1]);
    return Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === "string") : [];
  } catch {
    return [];
  }
}

async function migrateLegacyProviderStore(): Promise<ProviderStore | null> {
  const configPath = process.env.AGENTDU_CONFIG || path.join(app.getPath("appData"), "AgentDU", "config.toml");
  let source: string;
  try { source = await readFile(configPath, "utf8"); } catch { return { defaultProfile: null, profiles: [] }; }
  const headings = [...source.matchAll(/^\[profiles\.(?:"((?:\\.|[^"])*)"|([A-Za-z0-9_-]+))\]\s*$/gm)];
  const profiles: StoredProviderProfile[] = [];
  for (let index = 0; index < headings.length; index += 1) {
    const heading = headings[index];
    const name = heading[2] ?? JSON.parse(`"${heading[1]}"`) as string;
    const nextTable = source.indexOf("\n[", (heading.index ?? 0) + heading[0].length);
    const section = source.slice((heading.index ?? 0) + heading[0].length, nextTable < 0 ? undefined : nextTable);
    if (!/provider\s*=\s*["']openai-compatible["']/.test(section)) continue;
    const model = readTomlString(section, "model");
    if (!model) continue;
    const baseUrl = readTomlString(section, "base_url") ?? "https://api.openai.com/v1";
    const envName = readTomlString(section, "api_key_env");
    const apiKey = (envName ? process.env[envName] : undefined) || process.env.AGENTDU_API_KEY || process.env.OPENAI_API_KEY;
    let encryptedApiKey: string | undefined;
    if (apiKey) {
      if (!safeStorage.isEncryptionAvailable()) return null;
      encryptedApiKey = safeStorage.encryptString(apiKey).toString("base64");
    }
    profiles.push({
      name,
      baseUrl,
      model,
      encryptedApiKey,
      protocol: "OpenAI-compatible Chat Completions",
      wireApi: readTomlString(section, "wire_api") === "responses" ? "responses" : "chat_completions",
      streaming: readTomlBoolean(section, "streaming") ?? true,
      configuredCapabilities: readTomlStringArray(section, "capabilities"),
    });
  }
  const defaultProfile = readTomlString(source, "default_profile");
  return {
    defaultProfile: profiles.some((profile) => profile.name === defaultProfile) ? defaultProfile : profiles[0]?.name ?? null,
    profiles,
  };
}

async function synchronizeProviderConfig(environment: NodeJS.ProcessEnv): Promise<void> {
  const store = await readProviderStore();
  const providerStoreExists = await access(providerStorePath()).then(() => true).catch((error) => {
    if (isRecord(error) && error.code === "ENOENT") return false;
    throw error;
  });
  if (store.profiles.length === 0 && !providerStoreExists) {
    const legacyConfig = process.env.AGENTDU_CONFIG || path.join(environment.APPDATA || "", "AgentDU", "config.toml");
    if (process.env.AGENTDU_CONFIG) environment.AGENTDU_CONFIG = process.env.AGENTDU_CONFIG;
    for (const name of ["AGENTDU_API_KEY", "OPENAI_API_KEY"]) {
      if (process.env[name]) environment[name] = process.env[name];
    }
    try {
      const source = await readFile(legacyConfig, "utf8");
      for (const match of source.matchAll(/^\s*api_key_env\s*=\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*$/gm)) {
        const name = match[1];
        if (process.env[name]) environment[name] = process.env[name];
      }
    } catch {
      // A missing legacy configuration is the normal first-run state.
    }
    return;
  }
  const rows: string[] = [];
  if (store.defaultProfile) rows.push(`default_profile = ${tomlString(store.defaultProfile)}`, "");
  for (const profile of store.profiles) {
    const envName = providerEnvironmentName(profile.name);
    rows.push(`[profiles.${tomlString(profile.name)}]`);
    rows.push('provider = "openai-compatible"');
    rows.push(`base_url = ${tomlString(profile.baseUrl)}`);
    rows.push(`model = ${tomlString(profile.model)}`);
    rows.push(`wire_api = ${tomlString(profile.wireApi ?? "chat_completions")}`);
    rows.push(`api_key_env = ${tomlString(envName)}`);
    rows.push(`streaming = ${profile.streaming ?? true}`);
    rows.push(`capabilities = ${JSON.stringify(profile.configuredCapabilities ?? [])}`, "");
    if (profile.encryptedApiKey && safeStorage.isEncryptionAvailable()) {
      try {
        environment[envName] = safeStorage.decryptString(Buffer.from(profile.encryptedApiKey, "base64"));
      } catch {
        // Keep Runtime startup available if a saved credential cannot be decrypted.
      }
    }
  }
  const target = path.join(runtimeDataDirectory(), "providers.toml");
  const temporary = `${target}.tmp`;
  await writeFile(temporary, rows.join("\n"), { encoding: "utf8", mode: 0o600 });
  await rename(temporary, target);
  environment.AGENTDU_CONFIG = target;
}

function publicProviderProfiles(store: ProviderStore): Array<{ name: string; model: string; default: boolean; credentialAvailable: boolean; baseUrl: string; protocol: string; wireApi: "chat_completions" | "responses"; streaming: boolean; configuredCapabilities: string[] }> {
  return store.profiles.map((profile) => ({
    name: profile.name,
    model: profile.model,
    default: profile.name === store.defaultProfile,
    credentialAvailable: canDecryptCredential(profile.encryptedApiKey),
    baseUrl: profile.baseUrl,
    protocol: profile.wireApi === "responses" ? "OpenAI-compatible Responses API" : profile.protocol ?? "OpenAI-compatible Chat Completions",
    wireApi: profile.wireApi ?? "chat_completions",
    streaming: profile.streaming ?? true,
    configuredCapabilities: profile.configuredCapabilities ?? [],
  }));
}

function canDecryptCredential(encryptedApiKey: string | undefined): boolean {
  if (!encryptedApiKey || !safeStorage.isEncryptionAvailable()) return false;
  try { return safeStorage.decryptString(Buffer.from(encryptedApiKey, "base64")).length > 0; } catch { return false; }
}

async function filteredRuntimeEnvironment(): Promise<NodeJS.ProcessEnv> {
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
  await synchronizeProviderConfig(environment);
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
      env: await filteredRuntimeEnvironment(),
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
  client.on("event", (event: EventMessage) => {
    const payload = event.payload;
    const taskId = typeof payload.taskId === "string" ? payload.taskId : null;
    const sessionId = typeof payload.sessionId === "string" ? payload.sessionId : null;
    if (event.event === "task.accepted" && taskId && sessionId) {
      interruptedTaskCandidates.set(taskId, {
        sessionId,
        workspacePath: typeof payload.workspacePath === "string" ? payload.workspacePath : activeWorkspace,
      });
    }
    if (["task.completed", "task.failed", "task.cancelled", "task.interrupted"].includes(event.event) && taskId) {
      interruptedTaskCandidates.delete(taskId);
    }
    mainWindow?.webContents.send("runtime.event", event);
  });
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
  const expectedOrigin = trustedDevServerOrigin();
  if (!expectedOrigin) throw new Error("拒绝来自未配置开发服务器的 IPC 调用。");
  let frame: URL;
  try {
    frame = new URL(frameUrl);
  } catch {
    throw new Error("拒绝无效的 Renderer 地址。");
  }
  if (frame.origin !== expectedOrigin) {
    console.error(
      `[AgentDU] Rejected IPC origin ${frame.origin}. Expected loopback origin ${expectedOrigin}.`,
    );
    throw new Error("拒绝来自非开发界面的 IPC 调用。");
  }
}

function trustedDevServerOrigin(): string | null {
  const configuredDevUrl = process.env.AGENTDU_DESKTOP_DEV_SERVER_URL;
  if (!configuredDevUrl) return null;
  try {
    const url = new URL(configuredDevUrl);
    if (
      url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port ||
      url.username || url.password
    ) {
      return null;
    }
    return url.origin;
  } catch {
    return null;
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

  trustedInvoke("desktop.getStatus", async () => {
    const store = await readProviderStore();
    const profiles = publicProviderProfiles(store);
    return {
      ...runtimeStatus,
      providerProfiles: profiles.length > 0 ? profiles : runtimeStatus.providerProfiles,
      providerConfigured: profiles.length > 0 ? Boolean(store.defaultProfile) : runtimeStatus.providerConfigured,
    };
  });
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
  trustedInvoke("desktop.listWorkspaces", async () => {
    const result = await requireHost().request("workspace.list", {});
    return Array.isArray(result.workspaces) ? result.workspaces : [];
  });
  trustedInvoke("desktop.openRecentWorkspace", async (_event, payload) => {
    if (!isRecord(payload) || typeof payload.path !== "string") throw new Error("项目目录无效。");
    const host = requireHost();
    const recent = await host.request("workspace.list", {});
    const workspaces = Array.isArray(recent.workspaces) ? recent.workspaces : [];
    const selected = workspaces.find((item) => isRecord(item) && item.path === payload.path);
    if (!selected) throw new Error("项目不在最近工作区列表中，请通过文件选择器重新打开。");
    const opened = await host.request("workspace.open", { path: payload.path });
    activeWorkspace = typeof opened.path === "string" ? opened.path : null;
    return activeWorkspace;
  });
  trustedInvoke("desktop.inspectWorkspace", async () => requireHost().request("workspace.inspect", {}));
  trustedInvoke("desktop.listFiles", async (_event, payload) => {
    if (!isRecord(payload) || (payload.query !== undefined && typeof payload.query !== "string")) throw new Error("文件筛选条件无效。");
    const result = await requireHost().request("workspace.files", { query: payload.query ?? "", limit: 500 });
    return Array.isArray(result.files) ? result.files : [];
  });
  trustedInvoke("desktop.readFile", async (_event, payload) => {
    if (!isRecord(payload) || typeof payload.path !== "string" || payload.path.length > 32_768) throw new Error("文件路径无效。");
    return requireHost().request("file.read", { path: payload.path });
  });
  trustedInvoke("desktop.writeFile", async (_event, payload) => {
    assertProviderUpdateIdle();
    if (!isRecord(payload) || typeof payload.path !== "string" || payload.path.length > 32_768 || typeof payload.content !== "string" || payload.content.length > 120_000) throw new Error("文件写入内容无效或超过 120,000 字符。");
    if (typeof payload.sessionId !== "string" || payload.sessionId.length > 128) throw new Error("保存文件需要关联当前会话。");
    return requireHost().request("file.write", { path: payload.path, content: payload.content, sessionId: payload.sessionId }, 130_000);
  });
  trustedInvoke("desktop.getGitStatus", async () => {
    const result = await requireHost().request("git.status", {});
    return typeof result.status === "string" ? result.status : "";
  });
  trustedInvoke("desktop.getGitDiff", async (_event, payload) => {
    if (!isRecord(payload)) throw new Error("Diff 参数无效。");
    if (payload.path !== undefined && typeof payload.path !== "string") throw new Error("Diff 文件路径无效。");
    const result = await requireHost().request("git.diff", { path: payload.path, staged: payload.staged === true });
    return typeof result.diff === "string" ? result.diff : "";
  });
  trustedInvoke("desktop.saveProviderProfile", async (_event, payload) => {
    if (!isRecord(payload) || typeof payload.name !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(payload.name) || typeof payload.baseUrl !== "string" || typeof payload.model !== "string") throw new Error("Provider 档案名称、Endpoint 或 Model ID 无效。");
    let endpoint: URL;
    try { endpoint = new URL(payload.baseUrl); } catch { throw new Error("Endpoint 必须是有效的 HTTPS URL 或本机 HTTP URL。"); }
    if (endpoint.protocol !== "https:" && !(endpoint.protocol === "http:" && ["localhost", "127.0.0.1", "::1"].includes(endpoint.hostname))) throw new Error("Provider Endpoint 必须使用 HTTPS，本机服务可以使用 HTTP。");
    if (!payload.model.trim() || payload.model.length > 256 || payload.baseUrl.length > 2048) throw new Error("Model ID 或 Endpoint 长度无效。");
    if (payload.wireApi !== "chat_completions" && payload.wireApi !== "responses") throw new Error("API 协议必须选择 Chat Completions 或 Responses。");
    if (providerUpdateInProgress) throw new Error("另一个 Provider 配置变更正在处理中。");
    providerUpdateInProgress = true;
    try {
      const previous = await readProviderStore();
      const store = structuredClone(previous);
      if (store.profiles.length === 0) {
        const legacyProfiles = runtimeStatus.providerProfiles ?? [];
        for (const legacy of legacyProfiles) {
          if (!legacy.name || !legacy.model) continue;
          store.profiles.push({
            name: legacy.name,
            baseUrl: legacy.baseUrl ?? "https://api.openai.com/v1",
            model: legacy.model,
            protocol: legacy.protocol ?? "OpenAI-compatible Chat Completions",
            wireApi: legacy.wireApi ?? "chat_completions",
            streaming: legacy.streaming ?? true,
            configuredCapabilities: legacy.configuredCapabilities ?? [],
          });
        }
        if (!store.defaultProfile) store.defaultProfile = legacyProfiles.find((item) => item.default)?.name ?? null;
        const legacyProfile = legacyProfiles.find((item) => item.name === payload.name);
        if (legacyProfile?.credentialAvailable && !payload.apiKey && !safeStorage.isEncryptionAvailable()) {
          throw new Error("系统凭据加密不可用，无法安全迁移或替换当前 Provider 配置。");
        }
      }
      const existing = store.profiles.find((profile) => profile.name === payload.name);
      const updated: StoredProviderProfile = {
        name: payload.name,
        baseUrl: endpoint.toString().replace(/\/$/, ""),
        model: payload.model.trim(),
        encryptedApiKey: existing?.encryptedApiKey,
        protocol: payload.wireApi === "responses" ? "OpenAI-compatible Responses API" : "OpenAI-compatible Chat Completions",
        wireApi: payload.wireApi,
        streaming: existing?.streaming ?? true,
        configuredCapabilities: existing?.configuredCapabilities ?? [],
      };
      if (typeof payload.apiKey === "string" && payload.apiKey.length > 0) {
        if (payload.apiKey.length > 4096) throw new Error("API Key 长度超过限制。");
        if (!safeStorage.isEncryptionAvailable()) throw new Error("系统凭据加密不可用，无法安全保存 API Key。");
        updated.encryptedApiKey = safeStorage.encryptString(payload.apiKey).toString("base64");
      }
      if (payload.clearCredential === true) updated.encryptedApiKey = undefined;
      store.profiles = [...store.profiles.filter((profile) => profile.name !== payload.name), updated].sort((left, right) => left.name.localeCompare(right.name));
      if (payload.default === true || !store.defaultProfile) store.defaultProfile = payload.name;
      await ensureRuntimeCanRestart();
      await writeProviderStore(store);
      try {
        await restartProviderRuntime();
      } catch (error) {
        await rollbackProviderStore(previous, error);
      }
      return publicProviderProfiles(store);
    } finally {
      providerUpdateInProgress = false;
    }
  });
  trustedInvoke("desktop.deleteProviderProfile", async (_event, payload) => {
    if (!isRecord(payload) || typeof payload.name !== "string") throw new Error("Provider 档案名称无效。");
    if (providerUpdateInProgress) throw new Error("另一个 Provider 配置变更正在处理中。");
    providerUpdateInProgress = true;
    try {
      const previous = await readProviderStore();
      const store = structuredClone(previous);
      store.profiles = store.profiles.filter((profile) => profile.name !== payload.name);
      if (store.defaultProfile === payload.name) store.defaultProfile = store.profiles[0]?.name ?? null;
      await ensureRuntimeCanRestart();
      await writeProviderStore(store);
      try {
        await restartProviderRuntime();
      } catch (error) {
        await rollbackProviderStore(previous, error);
      }
      return publicProviderProfiles(store);
    } finally {
      providerUpdateInProgress = false;
    }
  });
  trustedInvoke("desktop.testProvider", async (_event, payload) => {
    if (!isRecord(payload) || typeof payload.name !== "string") throw new Error("Provider 档案名称无效。");
    const result = await requireHost().request("provider.test", { profile: payload.name }, 130_000);
    const failureKinds: ProviderTestFailureKind[] = [
      "authentication_failed",
      "access_denied",
      "model_or_endpoint_not_found",
      "rate_limited",
      "provider_unavailable",
      "http_error",
      "network_error",
      "invalid_response",
      "empty_response",
    ];
    const response: ProviderTestResult = {
      reachable: result.reachable === true,
      model: typeof result.model === "string" ? result.model : "",
      checkType: result.checkType === "responses" ? "responses" : "chat_completions",
      statusCode: typeof result.statusCode === "number" ? result.statusCode : null,
      failureKind: failureKinds.includes(result.failureKind as ProviderTestFailureKind)
        ? (result.failureKind as ProviderTestFailureKind)
        : null,
    };
    return response;
  });
  trustedInvoke("desktop.listSessions", async () => {
    const result = await requireHost().request("sessions.list", {});
    return Array.isArray(result.sessions) ? result.sessions : [];
  });
  trustedInvoke("desktop.createSession", async () => {
    const result = await requireHost().request("session.create", {});
    if (typeof result.sessionId !== "string") throw new Error("Runtime 未返回会话标识。");
    return result.sessionId;
  });
  trustedInvoke("desktop.readSession", async (_event, payload) => {
    if (!isRecord(payload) || typeof payload.sessionId !== "string" || payload.sessionId.length > 128) throw new Error("会话标识无效。");
    return requireHost().request("session.read", { sessionId: payload.sessionId });
  });
  trustedInvoke("desktop.runCommand", async (_event, payload) => {
    assertProviderUpdateIdle();
    if (!isRecord(payload) || typeof payload.command !== "string" || !payload.command.trim() || payload.command.length > 16_000 || payload.command.includes("\0")) throw new Error("命令必须为 1 至 16000 个字符，且不能包含 NUL 字符。");
    if (typeof payload.sessionId !== "string" || payload.sessionId.length > 128) throw new Error("运行命令需要关联当前会话。");
    const result = await requireHost().request("terminal.run", { command: payload.command, sessionId: payload.sessionId });
    if (typeof result.operationId !== "string") throw new Error("Runtime 未返回命令操作标识。");
    return { operationId: result.operationId, status: typeof result.status === "string" ? result.status : "pending" };
  });
  trustedInvoke("desktop.startTestTask", async () => {
    assertProviderUpdateIdle();
    const result = await requireHost().request("test.start", {});
    if (typeof result.taskId !== "string" || typeof result.sessionId !== "string") {
      throw new Error("Runtime 没有返回有效的测试任务标识。");
    }
    interruptedTaskCandidates.set(result.taskId, {
      sessionId: result.sessionId,
      workspacePath: typeof result.workspacePath === "string" ? result.workspacePath : null,
    });
    return { taskId: result.taskId, sessionId: result.sessionId };
  });
  trustedInvoke("desktop.startTask", async (_event, payload) => {
    assertProviderUpdateIdle();
    if (!isRecord(payload) || typeof payload.prompt !== "string" || payload.prompt.trim().length === 0 || payload.prompt.length > 16_000) {
      throw new Error("任务描述需为 1 至 16000 个字符。");
    }
    if (!activeWorkspace) throw new Error("请先通过系统文件选择器打开本地工作区。");
    if (typeof payload.sessionId !== "string" || payload.sessionId.length > 128) throw new Error("请先创建或恢复一个会话。");
    if (payload.profile !== undefined && typeof payload.profile !== "string") throw new Error("模型档案无效。");
    const result = await requireHost().request("turn.start", { prompt: payload.prompt, sessionId: payload.sessionId, profile: payload.profile });
    if (typeof result.taskId !== "string" || typeof result.sessionId !== "string") {
      throw new Error("Runtime 没有返回有效的任务标识。");
    }
    interruptedTaskCandidates.set(result.taskId, {
      sessionId: result.sessionId,
      workspacePath: typeof result.workspacePath === "string" ? result.workspacePath : activeWorkspace,
    });
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
    if (runtimeHost && !(await runtimeHost.waitForExit())) {
      throw new Error("旧的 Python Runtime 尚未退出，暂时无法安全重新启动。");
    }
    runtimeHost = null;
    runtimeHadUnexpectedExit = true;
    await startRuntimeHost();
    if (runtimeStatus.state !== "ready") {
      throw new Error(runtimeStatus.error ?? "Runtime 重启失败。");
    }
    const restartedHost = requireHost();
    const recoveryWarnings: string[] = [];
    for (const [taskId, task] of interruptedTaskCandidates) {
      const workspacePath = task.workspacePath ?? activeWorkspace;
      if (!workspacePath) {
        recoveryWarnings.push(`任务 ${taskId} 缺少工作区路径，无法核对其状态。`);
        continue;
      }
      try {
        const result = await restartedHost.request("task.recoverInterrupted", {
          taskId,
          sessionId: task.sessionId,
          workspacePath,
        });
        const recoveryStatus = typeof result.status === "string" ? result.status : "unknown";
        if (recoveryStatus === "session_busy") {
          recoveryWarnings.push(`任务 ${taskId} 的会话仍被占用，稍后可以重新核对。`);
        } else if (recoveryStatus === "not_found") {
          interruptedTaskCandidates.delete(taskId);
          recoveryWarnings.push(`任务 ${taskId} 的会话记录不存在。`);
        } else if (recoveryStatus === "unknown") {
          recoveryWarnings.push(`任务 ${taskId} 返回了无法识别的恢复状态。`);
        } else {
          interruptedTaskCandidates.delete(taskId);
        }
      } catch (error) {
        recoveryWarnings.push(
          `任务 ${taskId} 的状态核对失败：${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }
    if (activeWorkspace) {
      try {
        const opened = await restartedHost.request("workspace.open", { path: activeWorkspace });
        activeWorkspace = typeof opened.path === "string" ? opened.path : activeWorkspace;
      } catch (error) {
        recoveryWarnings.push(
          `Runtime 已恢复，但项目重新连接失败：${error instanceof Error ? error.message : String(error)}`,
        );
      }
    }
    return { recoveryWarnings };
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

  const devServerUrl = !app.isPackaged ? trustedDevServerOrigin() : null;
  if (devServerUrl) {
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

function assertProviderUpdateIdle(): void {
  if (providerUpdateInProgress) throw new Error("Provider 配置正在重新载入，请稍后再开始操作。");
}

async function ensureRuntimeCanRestart(): Promise<void> {
  const current = runtimeHost;
  if (!current || runtimeStatus.state !== "ready") return;
  const status = await current.request("host.health", {});
  if (typeof status.activeTasks === "number" && status.activeTasks > 0) {
    throw new Error("有任务或文件操作正在运行，请等待完成后再修改 Provider 配置。");
  }
}

async function reopenActiveWorkspace(): Promise<void> {
  if (!activeWorkspace) return;
  const opened = await requireHost().request("workspace.open", { path: activeWorkspace });
  activeWorkspace = typeof opened.path === "string" ? opened.path : activeWorkspace;
}

async function rollbackProviderStore(previous: ProviderStore, cause: unknown): Promise<never> {
  const originalError = cause instanceof Error ? cause.message : String(cause);
  try {
    await shutdownRuntime();
    await writeProviderStore(previous);
    runtimeHadUnexpectedExit = false;
    await startRuntimeHost();
    if (runtimeStatus.state !== "ready") throw new Error(runtimeStatus.error ?? "Runtime 未能恢复到可用状态。");
    await reopenActiveWorkspace();
  } catch (recoveryError) {
    const recoveryMessage = recoveryError instanceof Error ? recoveryError.message : String(recoveryError);
    throw new Error(`Provider 更新失败：${originalError}。恢复旧配置也失败：${recoveryMessage}`);
  }
  throw new Error(`Provider 更新失败，旧配置已恢复且 Runtime 已重启：${originalError}`);
}

async function restartProviderRuntime(): Promise<void> {
  const current = runtimeHost;
  if (current && runtimeStatus.state === "ready") {
    const status = await current.request("host.health", {});
    if (typeof status.activeTasks === "number" && status.activeTasks > 0) {
      throw new Error("有任务或文件操作正在运行，请等待完成后再修改 Provider 配置。");
    }
  }
  await shutdownRuntime();
  runtimeHadUnexpectedExit = false;
  await startRuntimeHost();
  if (runtimeStatus.state !== "ready") throw new Error(runtimeStatus.error ?? "Runtime 重启失败。");
  await reopenActiveWorkspace();
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
