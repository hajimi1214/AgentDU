import { contextBridge, ipcRenderer } from "electron";
import type { EventMessage, ProviderProfileInput, ProviderTestResult, RuntimeStatus, SessionEvent, SessionMessage, SessionSummary, WorkspaceEntry, WorkspaceFile } from "../shared/protocol";

export type AgentDUDesktopApi = {
  getStatus(): Promise<RuntimeStatus>;
  openWorkspace(): Promise<string | null>;
  openRecentWorkspace(path: string): Promise<string | null>;
  listWorkspaces(): Promise<WorkspaceEntry[]>;
  inspectWorkspace(): Promise<{ path: string; name: string; files: WorkspaceFile[]; gitStatus: string; gitError: string | null }>;
  listFiles(query?: string): Promise<WorkspaceFile[]>;
  readFile(path: string): Promise<{ path: string; content: string }>;
  writeFile(path: string, content: string, sessionId: string): Promise<{ path: string; status: string; operationId?: string }>;
  getGitStatus(): Promise<string>;
  getGitDiff(path?: string, staged?: boolean): Promise<string>;
  saveProviderProfile(profile: ProviderProfileInput): Promise<RuntimeStatus["providerProfiles"]>;
  deleteProviderProfile(name: string): Promise<RuntimeStatus["providerProfiles"]>;
  testProvider(name: string): Promise<ProviderTestResult>;
  listSessions(): Promise<SessionSummary[]>;
  createSession(): Promise<string>;
  readSession(sessionId: string): Promise<{ sessionId: string; events: SessionEvent[]; messages: SessionMessage[] }>;
  runCommand(command: string, sessionId: string): Promise<{ operationId: string; status: string }>;
  startTestTask(): Promise<{ taskId: string; sessionId: string }>;
  startTask(prompt: string, sessionId: string, profile?: string): Promise<{ taskId: string; sessionId: string }>;
  cancelTask(taskId: string): Promise<{ status: string }>;
  respondToPermission(approvalId: string, decision: "allow" | "deny"): Promise<void>;
  restartRuntime(): Promise<{ recoveryWarnings: string[] }>;
  onEvent(callback: (event: EventMessage) => void): () => void;
  onStatus(callback: (status: RuntimeStatus) => void): () => void;
};

const api: AgentDUDesktopApi = {
  getStatus: () => ipcRenderer.invoke("desktop.getStatus"),
  openWorkspace: () => ipcRenderer.invoke("desktop.openWorkspace"),
  openRecentWorkspace: (path) => ipcRenderer.invoke("desktop.openRecentWorkspace", { path }),
  listWorkspaces: () => ipcRenderer.invoke("desktop.listWorkspaces"),
  inspectWorkspace: () => ipcRenderer.invoke("desktop.inspectWorkspace"),
  listFiles: (query) => ipcRenderer.invoke("desktop.listFiles", { query }),
  readFile: (path) => ipcRenderer.invoke("desktop.readFile", { path }),
  writeFile: (path, content, sessionId) => ipcRenderer.invoke("desktop.writeFile", { path, content, sessionId }),
  getGitStatus: () => ipcRenderer.invoke("desktop.getGitStatus"),
  getGitDiff: (path, staged) => ipcRenderer.invoke("desktop.getGitDiff", { path, staged }),
  saveProviderProfile: (profile) => ipcRenderer.invoke("desktop.saveProviderProfile", profile),
  deleteProviderProfile: (name) => ipcRenderer.invoke("desktop.deleteProviderProfile", { name }),
  testProvider: (name) => ipcRenderer.invoke("desktop.testProvider", { name }),
  listSessions: () => ipcRenderer.invoke("desktop.listSessions"),
  createSession: () => ipcRenderer.invoke("desktop.createSession"),
  readSession: (sessionId) => ipcRenderer.invoke("desktop.readSession", { sessionId }),
  runCommand: (command, sessionId) => ipcRenderer.invoke("desktop.runCommand", { command, sessionId }),
  startTestTask: () => ipcRenderer.invoke("desktop.startTestTask"),
  startTask: (prompt, sessionId, profile) => ipcRenderer.invoke("desktop.startTask", { prompt, sessionId, profile }),
  cancelTask: (taskId) => ipcRenderer.invoke("desktop.cancelTask", { taskId }),
  respondToPermission: (approvalId, decision) =>
    ipcRenderer.invoke("desktop.respondToPermission", { approvalId, decision }),
  restartRuntime: () => ipcRenderer.invoke("desktop.restartRuntime"),
  onEvent: (callback) => subscribe("runtime.event", callback),
  onStatus: (callback) => subscribe("runtime.status", callback),
};

function subscribe<T>(channel: string, callback: (payload: T) => void): () => void {
  const listener = (_event: Electron.IpcRendererEvent, payload: T) => callback(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld("agentdu", api);
