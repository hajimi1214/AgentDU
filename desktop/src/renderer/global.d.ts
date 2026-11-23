import type { AgentDUDesktopApi } from "../preload";

declare global {
  interface Window {
    agentdu: AgentDUDesktopApi;
  }
}

export {};
