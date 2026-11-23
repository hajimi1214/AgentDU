import { EventEmitter } from "node:events";
import { StringDecoder } from "node:string_decoder";
import { randomUUID } from "node:crypto";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import {
  MAX_FRAME_BYTES,
  PROTOCOL_VERSION,
  parseHostMessage,
  type EventMessage,
  type HostState,
  type RuntimeStatus,
} from "../shared/protocol";

type RequestResult = Record<string, unknown>;
type PendingRequest = {
  resolve(value: RequestResult): void;
  reject(error: Error): void;
  timeout: NodeJS.Timeout;
};

export class RuntimeHostClient extends EventEmitter {
  private readonly child: ChildProcessWithoutNullStreams;
  private readonly pending = new Map<string, PendingRequest>();
  private readonly decoder = new StringDecoder("utf8");
  private stdoutBuffer = "";
  private lastEventSequence = 0;
  private state: HostState = "starting";
  private status: RuntimeStatus = { state: "starting" };
  private closedIntentionally = false;

  constructor(child: ChildProcessWithoutNullStreams) {
    super();
    this.child = child;
    child.stdout.on("data", (chunk: Buffer) => this.readStdout(chunk));
    child.stderr.on("data", (chunk: Buffer) => this.emit("stderr", chunk));
    child.on("error", (error) => this.fail(`Python Runtime could not start: ${error.message}`));
    child.on("exit", (code, signal) => this.handleExit(code, signal));
  }

  async initialize(): Promise<RuntimeStatus> {
    try {
      const result = await this.requestInternal("host.initialize", {});
      this.setStatus({
        state: "ready",
        runtimeVersion: readString(result.runtimeVersion),
        providerConfigured: result.providerConfigured === true,
        providerProfiles: Array.isArray(result.providerProfiles)
          ? result.providerProfiles.map((profile) => ({
              name: readString((profile as Record<string, unknown>).name) ?? "未命名档案",
              model: readString((profile as Record<string, unknown>).model) ?? null,
              default: (profile as Record<string, unknown>).default === true,
              credentialAvailable: (profile as Record<string, unknown>).credentialAvailable === true,
            }))
          : [],
      });
      return this.getStatus();
    } catch (error) {
      this.fail(error instanceof Error ? error.message : "Runtime 握手失败。");
      throw error;
    }
  }

  getStatus(): RuntimeStatus {
    return { ...this.status };
  }

  request(method: string, params: Record<string, unknown>): Promise<RequestResult> {
    if (this.state !== "ready") {
      return Promise.reject(new Error("Python Runtime 当前不可用。"));
    }
    return this.requestInternal(method, params);
  }

  async shutdown(): Promise<void> {
    if (this.child.exitCode !== null || this.child.killed) return;
    this.closedIntentionally = true;
    try {
      if (this.state === "ready") await this.requestInternal("host.shutdown", {}, 2_000);
    } catch {
      // The process is still closed below. Shutdown failures are reflected in the exit status.
    }
    this.child.stdin.end();
    await Promise.race([
      new Promise<void>((resolve) => this.child.once("exit", () => resolve())),
      new Promise<void>((resolve) => setTimeout(resolve, 2_500)),
    ]);
    if (this.child.exitCode === null && !this.child.killed) this.child.kill();
  }

  terminateForDiagnostics(): void {
    this.closedIntentionally = false;
    this.child.kill();
  }

  private requestInternal(
    method: string,
    params: Record<string, unknown>,
    timeoutMs = 10_000,
  ): Promise<RequestResult> {
    const requestId = randomUUID();
    const frame = Buffer.from(
      `${JSON.stringify({
        type: "request",
        protocolVersion: PROTOCOL_VERSION,
        requestId,
        method,
        params,
      })}\n`,
      "utf8",
    );
    if (frame.byteLength > MAX_FRAME_BYTES) {
      return Promise.reject(new Error("IPC 请求超过 1 MiB 限制。"));
    }

    return new Promise<RequestResult>((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(requestId);
        reject(new Error(`Runtime 请求超时：${method}`));
      }, timeoutMs);
      this.pending.set(requestId, { resolve, reject, timeout });
      this.child.stdin.write(frame, (error) => {
        if (!error) return;
        const pending = this.pending.get(requestId);
        if (!pending) return;
        clearTimeout(pending.timeout);
        this.pending.delete(requestId);
        pending.reject(new Error(`Runtime IPC 写入失败：${error.message}`));
      });
    });
  }

  private readStdout(chunk: Buffer): void {
    this.stdoutBuffer += this.decoder.write(chunk);
    while (true) {
      const newline = this.stdoutBuffer.indexOf("\n");
      if (newline < 0) {
        if (Buffer.byteLength(this.stdoutBuffer, "utf8") > MAX_FRAME_BYTES) {
          this.fail("Runtime 输出超过 1 MiB 协议帧限制。");
        }
        return;
      }
      const line = this.stdoutBuffer.slice(0, newline);
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
      if (Buffer.byteLength(line, "utf8") > MAX_FRAME_BYTES) {
        this.fail("Runtime 输出超过 1 MiB 协议帧限制。");
        return;
      }
      try {
        this.handleMessage(parseHostMessage(line));
      } catch (error) {
        this.fail(error instanceof Error ? error.message : "Runtime 输出了无效 IPC 消息。");
        return;
      }
    }
  }

  private handleMessage(message: ReturnType<typeof parseHostMessage>): void {
    if (message.type === "response") {
      const pending = this.pending.get(message.requestId);
      if (!pending) return;
      clearTimeout(pending.timeout);
      this.pending.delete(message.requestId);
      if (message.error) {
        pending.reject(new Error(message.error.message));
      } else {
        pending.resolve(message.result ?? {});
      }
      return;
    }

    if (message.sequence <= this.lastEventSequence) {
      throw new Error("Runtime 事件序号重复或倒退。");
    }
    this.lastEventSequence = message.sequence;
    this.emit("event", message as EventMessage);
  }

  private handleExit(code: number | null, signal: NodeJS.Signals | null): void {
    this.rejectPending(new Error("Python Runtime 已退出。"));
    if (this.closedIntentionally) {
      this.setStatus({ state: "stopped", exitCode: code });
      return;
    }
    const details = signal ? `信号 ${signal}` : `退出码 ${code ?? "未知"}`;
    this.fail(`Python Runtime 异常退出（${details}）。活动任务不会自动重放。`);
    this.setStatus({ ...this.status, exitCode: code, recoveredAfterCrash: true });
  }

  private fail(message: string): void {
    if (this.state === "error" && this.status.error) return;
    this.setStatus({ state: "error", error: message });
    this.rejectPending(new Error(message));
    if (!this.child.killed && this.child.exitCode === null) this.child.kill();
  }

  private rejectPending(error: Error): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timeout);
      pending.reject(error);
    }
    this.pending.clear();
  }

  private setStatus(status: RuntimeStatus): void {
    this.state = status.state;
    this.status = status;
    this.emit("status", this.getStatus());
  }
}

function readString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}
