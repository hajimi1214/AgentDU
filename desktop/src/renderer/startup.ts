import type { RuntimeStatus } from "../shared/protocol";

export function observeRuntimeStartup(
  getStatus: () => Promise<RuntimeStatus>,
  subscribe: (callback: (status: RuntimeStatus) => void) => () => void,
  onStatus: (status: RuntimeStatus) => void,
  onReady: () => void,
  onError: (error: unknown) => void,
): () => void {
  let active = true;
  let readyHandled = false;

  const applyStatus = (status: RuntimeStatus) => {
    if (!active) return;
    onStatus(status);
    if (status.state === "ready" && !readyHandled) {
      readyHandled = true;
      onReady();
    }
  };

  const unsubscribe = subscribe(applyStatus);
  void getStatus().then(applyStatus).catch((error: unknown) => {
    if (active && !readyHandled) onError(error);
  });

  return () => {
    active = false;
    unsubscribe();
  };
}
