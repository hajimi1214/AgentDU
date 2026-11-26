import { describe, expect, it } from "vitest";
import {
  initialTaskLifecycleState,
  reduceTaskLifecycle,
  type TaskLifecycleState,
} from "./task-lifecycle";

function reduce(
  state: TaskLifecycleState,
  ...actions: Parameters<typeof reduceTaskLifecycle>[1][]
): TaskLifecycleState {
  return actions.reduce(reduceTaskLifecycle, state);
}

describe("reduceTaskLifecycle", () => {
  it("tracks task acceptance and terminal events", () => {
    const state = reduce(
      initialTaskLifecycleState,
      { type: "accepted", taskId: "task-1" },
      { type: "finished", taskId: "task-1" },
    );

    expect(state.activeTaskId).toBeNull();
    expect(state.terminalTaskIds).toEqual(["task-1"]);
  });

  it("does not revive a task when the start response arrives after its terminal event", () => {
    const state = reduce(
      initialTaskLifecycleState,
      { type: "accepted", taskId: "task-1" },
      { type: "finished", taskId: "task-1" },
      { type: "request-resolved", taskId: "task-1" },
    );

    expect(state.activeTaskId).toBeNull();
    expect(state.terminalTaskIds).toEqual(["task-1"]);
  });

  it("keeps the current task active when an unrelated task finishes", () => {
    const state = reduce(
      initialTaskLifecycleState,
      { type: "accepted", taskId: "task-2" },
      { type: "finished", taskId: "task-1" },
    );

    expect(state.activeTaskId).toBe("task-2");
    expect(state.terminalTaskIds).toEqual(["task-1"]);
  });

  it("marks an active task terminal when the Runtime process is lost", () => {
    const state = reduce(
      initialTaskLifecycleState,
      { type: "request-resolved", taskId: "task-1" },
      { type: "runtime-lost" },
    );

    expect(state.activeTaskId).toBeNull();
    expect(state.terminalTaskIds).toEqual(["task-1"]);
  });
});
