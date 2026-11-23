import { contextBridge, ipcRenderer } from "electron";
import type { EventMessage, RuntimeStatus } from "../shared/protocol";

export type AgentDUDesktopApi = {
  getStatus(): Promise<RuntimeStatus>;
  openWorkspace(): Promise<string | null>;
  startTestTask(): Promise<{ taskId: string; sessionId: string }>;
  startTask(prompt: string): Promise<{ taskId: string; sessionId: string }>;
  cancelTask(taskId: string): Promise<{ status: string }>;
  respondToPermission(approvalId: string, decision: "allow" | "deny"): Promise<void>;
  restartRuntime(): Promise<void>;
  onEvent(callback: (event: EventMessage) => void): () => void;
  onStatus(callback: (status: RuntimeStatus) => void): () => void;
};

const api: AgentDUDesktopApi = {
  getStatus: () => ipcRenderer.invoke("desktop.getStatus"),
  openWorkspace: () => ipcRenderer.invoke("desktop.openWorkspace"),
  startTestTask: () => ipcRenderer.invoke("desktop.startTestTask"),
  startTask: (prompt) => ipcRenderer.invoke("desktop.startTask", { prompt }),
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
