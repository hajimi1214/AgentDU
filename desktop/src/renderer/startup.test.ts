import { describe, expect, it, vi } from "vitest";
import type { RuntimeStatus } from "../shared/protocol";
import { observeRuntimeStartup } from "./startup";

function createStatusSubscription() {
  let listener: ((status: RuntimeStatus) => void) | null = null;
  const unsubscribe = vi.fn(() => {
    listener = null;
  });
  return {
    subscribe: (callback: (status: RuntimeStatus) => void) => {
      listener = callback;
      return unsubscribe;
    },
    publish: (status: RuntimeStatus) => listener?.(status),
    unsubscribe,
  };
}

describe("observeRuntimeStartup", () => {
  it("waits for the ready event and loads recent workspaces only once", async () => {
    let resolveStatus!: (status: RuntimeStatus) => void;
    const initialStatus = new Promise<RuntimeStatus>((resolve) => {
      resolveStatus = resolve;
    });
    const channel = createStatusSubscription();
    const onStatus = vi.fn();
    const onReady = vi.fn();
    const onError = vi.fn();
    const stop = observeRuntimeStartup(
      () => initialStatus,
      channel.subscribe,
      onStatus,
      onReady,
      onError,
    );

    channel.publish({ state: "starting" });
    expect(onReady).not.toHaveBeenCalled();

    channel.publish({ state: "ready", runtimeVersion: "0.6.0" });
    resolveStatus({ state: "ready", runtimeVersion: "0.6.0" });
    await initialStatus;
    await Promise.resolve();

    expect(onReady).toHaveBeenCalledTimes(1);
    expect(onStatus).toHaveBeenCalledTimes(3);
    expect(onError).not.toHaveBeenCalled();
    stop();
    expect(channel.unsubscribe).toHaveBeenCalledTimes(1);
  });

  it("loads immediately when the initial status is already ready", async () => {
    const channel = createStatusSubscription();
    const onReady = vi.fn();
    observeRuntimeStartup(
      async () => ({ state: "ready", runtimeVersion: "0.6.0" }),
      channel.subscribe,
      vi.fn(),
      onReady,
      vi.fn(),
    );

    await Promise.resolve();

    expect(onReady).toHaveBeenCalledTimes(1);
  });

  it("does not load workspaces when the runtime fails to start", async () => {
    const channel = createStatusSubscription();
    const onReady = vi.fn();
    const onError = vi.fn();
    const failure = new Error("Runtime 启动失败");
    observeRuntimeStartup(
      async () => ({ state: "error", error: failure.message }),
      channel.subscribe,
      vi.fn(),
      onReady,
      onError,
    );

    await Promise.resolve();

    expect(onReady).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("ignores a late initial status after the subscription is stopped", async () => {
    let resolveStatus!: (status: RuntimeStatus) => void;
    const initialStatus = new Promise<RuntimeStatus>((resolve) => {
      resolveStatus = resolve;
    });
    const channel = createStatusSubscription();
    const onReady = vi.fn();
    const onStatus = vi.fn();
    const stop = observeRuntimeStartup(
      () => initialStatus,
      channel.subscribe,
      onStatus,
      onReady,
      vi.fn(),
    );

    stop();
    resolveStatus({ state: "ready", runtimeVersion: "0.6.0" });
    await initialStatus;
    await Promise.resolve();

    expect(onReady).not.toHaveBeenCalled();
    expect(onStatus).not.toHaveBeenCalled();
  });
});
