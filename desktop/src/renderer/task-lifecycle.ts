export type TaskLifecycleState = {
  activeTaskId: string | null;
  terminalTaskIds: string[];
};

export type TaskLifecycleAction =
  | { type: "accepted"; taskId: string }
  | { type: "request-resolved"; taskId: string }
  | { type: "finished"; taskId: string }
  | { type: "runtime-lost" };

export const initialTaskLifecycleState: TaskLifecycleState = {
  activeTaskId: null,
  terminalTaskIds: [],
};

const MAX_REMEMBERED_TERMINAL_TASKS = 64;

export function reduceTaskLifecycle(
  state: TaskLifecycleState,
  action: TaskLifecycleAction,
): TaskLifecycleState {
  if (action.type === "accepted" || action.type === "request-resolved") {
    if (state.terminalTaskIds.includes(action.taskId)) return state;
    return { ...state, activeTaskId: action.taskId };
  }

  if (action.type === "runtime-lost") {
    if (!state.activeTaskId) return state;
    return finishTask(state, state.activeTaskId);
  }

  return finishTask(state, action.taskId);
}

function finishTask(state: TaskLifecycleState, taskId: string): TaskLifecycleState {
  const terminalTaskIds = [
    ...state.terminalTaskIds.filter((item) => item !== taskId),
    taskId,
  ].slice(-MAX_REMEMBERED_TERMINAL_TASKS);

  return {
    activeTaskId: state.activeTaskId === taskId ? null : state.activeTaskId,
    terminalTaskIds,
  };
}
