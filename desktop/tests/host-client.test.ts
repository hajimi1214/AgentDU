import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { afterEach, describe, expect, it } from "vitest";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { RuntimeHostClient } from "../src/main/host-client";

class FakeRuntimeChild extends EventEmitter {
  readonly stdin = new PassThrough();
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  exitCode: number | null = null;
  killed = false;

  constructor(private readonly respondToInitialize = true) {
    super();
    this.stdin.on("end", () => {
      if (this.exitCode === null) this.crash(0);
    });
    this.stdin.on("data", (frame: Buffer) => {
      if (!this.respondToInitialize) return;
      const request = JSON.parse(frame.toString("utf8")) as { requestId: string };
      this.stdout.write(
        `${JSON.stringify({
          type: "response",
          protocolVersion: 1,
          requestId: request.requestId,
          result: {
            runtimeVersion: "0.6.0",
            providerConfigured: false,
            providerProfiles: [],
          },
        })}\n`,
      );
    });
  }

  kill(): boolean {
    if (this.exitCode !== null) return false;
    this.killed = true;
    this.exitCode = 1;
    this.emit("exit", 1, null);
    return true;
  }

  crash(code: number): void {
    this.exitCode = code;
    this.stdout.end();
    this.stderr.end();
    this.emit("exit", code, null);
  }
}

const clients: RuntimeHostClient[] = [];

function createClient(child: FakeRuntimeChild): RuntimeHostClient {
  const client = new RuntimeHostClient(child as unknown as ChildProcessWithoutNullStreams);
  clients.push(client);
  return client;
}

afterEach(async () => {
  await Promise.all(clients.splice(0).map((client) => client.shutdown()));
});

describe("Runtime Host process client", () => {
  it("completes the versioned initialization handshake", async () => {
    const client = createClient(new FakeRuntimeChild());

    await expect(client.initialize()).resolves.toMatchObject({
      state: "ready",
      runtimeVersion: "0.6.0",
      providerConfigured: false,
    });
  });

  it("surfaces an unexpected Runtime process exit for recovery UI", async () => {
    const child = new FakeRuntimeChild(false);
    const client = createClient(child);
    const initialization = client.initialize();
    child.crash(71);

    await expect(initialization).rejects.toThrow("Python Runtime 已退出。");
    expect(client.getStatus()).toMatchObject({
      state: "error",
      exitCode: 71,
      recoveredAfterCrash: true,
    });
  });

  it("waits for an exited Runtime process before it can be restarted", async () => {
    const child = new FakeRuntimeChild();
    const client = createClient(child);
    await client.initialize();

    const exit = client.waitForExit(100);
    child.crash(71);

    await expect(exit).resolves.toBe(true);
  });

  it("keeps waiting when the Runtime process has not exited", async () => {
    const client = createClient(new FakeRuntimeChild());
    await client.initialize();

    await expect(client.waitForExit(0)).resolves.toBe(false);
  });

  it("surfaces a Runtime process launch error", async () => {
    const child = new FakeRuntimeChild(false);
    const client = createClient(child);
    const initialization = client.initialize();
    child.emit("error", new Error("spawn failed"));

    await expect(initialization).rejects.toThrow("Python Runtime could not start: spawn failed");
    expect(client.getStatus()).toMatchObject({ state: "error", recoveredAfterCrash: true });
  });

  it("kills the Runtime and reports malformed stdout as a protocol error", async () => {
    const child = new FakeRuntimeChild();
    const client = createClient(child);
    await client.initialize();

    child.stdout.write("not-json\n");

    expect(child.killed).toBe(true);
    expect(client.getStatus()).toMatchObject({
      state: "error",
      error: expect.stringContaining("JSON"),
      recoveredAfterCrash: true,
    });
  });
});
