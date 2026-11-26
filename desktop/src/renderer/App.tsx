import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import type {
  PermissionRequestEvent,
  ProviderProfileInput,
  RuntimeStatus,
  SessionEvent,
  SessionMessage,
  SessionSummary,
  WorkspaceEntry,
  WorkspaceFile,
} from "../shared/protocol";
import { observeRuntimeStartup } from "./startup";
import { initialTaskLifecycleState, reduceTaskLifecycle } from "./task-lifecycle";

type Activity = {
  id: string;
  sequence: number;
  event: string;
  occurredAt: string;
  payload: Record<string, unknown>;
};

type Project = { path: string; name: string; files: WorkspaceFile[]; gitStatus: string; gitError: string | null };
type InspectorTab = "files" | "diff" | "terminal";
type TerminalRecord = { operationId: string; command: string; workingDirectory: string; status: string; exitCode: number | null; output: string };
type RuntimeCheckStatus = "idle" | "starting" | "running" | "cancelling" | "completed" | "cancelled" | "failed";

const RUNTIME_CHECK_STATUS_LABELS: Record<RuntimeCheckStatus, string> = {
  idle: "未运行",
  starting: "正在启动",
  running: "正在流式运行",
  cancelling: "正在请求取消",
  completed: "运行完成",
  cancelled: "已取消",
  failed: "运行失败",
};

const EVENT_LABELS: Record<string, string> = {
  "host.ready": "Runtime 已连接",
  "workspace.opened": "项目已打开",
  "task.accepted": "任务已接收",
  run_started: "Agent 开始运行",
  model_request: "模型请求",
  assistant_delta: "模型回复",
  assistant_message: "消息已保存",
  run_completed: "任务完成",
  "task.completed": "任务完成",
  run_stopped: "任务已停止",
  tool_started: "工具开始",
  tool_finished: "工具结束",
  "permission.requested": "等待操作审批",
  "permission.resolved": "审批已处理",
  "workspace.write.finished": "文件写入结果",
  "terminal.command.started": "终端命令已请求",
  "terminal.command.finished": "终端命令结果",
  "task.cancel_requested": "正在取消",
  "task.cancelled": "任务已取消",
  "task.failed": "任务失败",
  "protocol.error": "协议错误",
};

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
}

function describeActivity(activity: Activity): string {
  const { event, payload } = activity;
  if (event === "assistant_delta") return String(payload.text ?? "收到输出");
  if (event === "model_request") return `第 ${String(payload.step ?? "")} 轮模型请求`;
  if (event === "tool_started" || event === "tool_finished") return String(payload.tool_name ?? "工具");
  if (event === "task.failed") return String(payload.message ?? "任务失败");
  if (event === "permission.requested") return `${String(payload.operation ?? "需要审批")} · ${String((payload.arguments as Record<string, unknown> | undefined)?.path ?? "")}`;
  if (event === "workspace.write.finished") return `${String(payload.status ?? "")} · ${String(payload.path ?? "")}`;
  if (event === "task.accepted") return `会话 ${String(payload.sessionId ?? "")} 已启动`;
  return String(payload.message ?? payload.status ?? payload.reason ?? "状态已更新");
}

function messageText(message: SessionMessage): string {
  return typeof message.content === "string" ? message.content : "";
}

function eventToActivity(item: SessionEvent, index: number): Activity {
  return {
    id: `${item.seq}-${index}`,
    sequence: item.seq,
    event: item.kind,
    occurredAt: item.occurred_at,
    payload: item.payload,
  };
}

export function App() {
  const [status, setStatus] = useState<RuntimeStatus>({ state: "starting" });
  const [projects, setProjects] = useState<WorkspaceEntry[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const [activities, setActivities] = useState<Activity[]>([]);
  const [streamingText, setStreamingText] = useState("");
  const [taskLifecycle, dispatchTaskLifecycle] = useReducer(reduceTaskLifecycle, initialTaskLifecycleState);
  const [taskStarting, setTaskStarting] = useState(false);
  const [runtimeCheckStatus, setRuntimeCheckStatus] = useState<RuntimeCheckStatus>("idle");
  const [runtimeCheckTaskId, setRuntimeCheckTaskId] = useState<string | null>(null);
  const [runtimeCheckOutput, setRuntimeCheckOutput] = useState("");
  const activeTaskId = taskLifecycle.activeTaskId;
  const busy = taskStarting || activeTaskId !== null;
  const [prompt, setPrompt] = useState("");
  const [permission, setPermission] = useState<PermissionRequestEvent | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("files");
  const [inspectorVisible, setInspectorVisible] = useState(false);
  const [fileQuery, setFileQuery] = useState("");
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState("");
  const [fileDraft, setFileDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [pendingFileWriteId, setPendingFileWriteId] = useState<string | null>(null);
  const [terminalCommand, setTerminalCommand] = useState("");
  const [terminalBusy, setTerminalBusy] = useState(false);
  const [terminalHistory, setTerminalHistory] = useState<TerminalRecord[]>([]);
  const pendingFileWriteRef = useRef<string | null>(null);
  const runtimeCheckTaskIdRef = useRef<string | null>(null);
  const runtimeCheckSessionIdRef = useRef<string | null>(null);
  const fileDraftRef = useRef(fileDraft);
  const initialWorkspaceLoaded = useRef(false);
  pendingFileWriteRef.current = pendingFileWriteId;
  fileDraftRef.current = fileDraft;
  const [gitDiff, setGitDiff] = useState("");
  const [stagedDiff, setStagedDiff] = useState(false);
  const [selectedProfile, setSelectedProfile] = useState("");
  const [showProviderSettings, setShowProviderSettings] = useState(false);
  const [providerName, setProviderName] = useState("");
  const [providerBaseUrl, setProviderBaseUrl] = useState("https://api.openai.com/v1");
  const [providerModel, setProviderModel] = useState("");
  const [providerWireApi, setProviderWireApi] = useState<"chat_completions" | "responses">("chat_completions");
  const [providerKey, setProviderKey] = useState("");
  const [makeDefault, setMakeDefault] = useState(true);
  const [providerTest, setProviderTest] = useState<string | null>(null);
  const [providerBusy, setProviderBusy] = useState(false);

  const profiles = status.providerProfiles ?? [];
  const currentProfile = profiles.find((item) => item.name === selectedProfile) ?? profiles.find((item) => item.default) ?? profiles[0];
  const fileDirty = editing && fileDraft !== fileContent;
  const workspaceBusy = busy || terminalBusy || Boolean(pendingFileWriteId);

  useEffect(() => {
    let cancelled = false;
    let loadingRecentWorkspaces = false;

    const loadRecentWorkspaces = async () => {
      if (cancelled || loadingRecentWorkspaces || initialWorkspaceLoaded.current) return;
      loadingRecentWorkspaces = true;
      try {
        const recent = await window.agentdu.listWorkspaces();
        if (cancelled) return;
        initialWorkspaceLoaded.current = true;
        setProjects(recent);
        if (recent.length > 0) await selectRecentProject(recent[0]);
        else setNotice(null);
      } catch (error) {
        if (!cancelled) setNotice(error instanceof Error ? error.message : "无法读取最近打开的项目。");
      } finally {
        loadingRecentWorkspaces = false;
      }
    };

    const stopStatus = observeRuntimeStartup(
      () => window.agentdu.getStatus(),
      (callback) => window.agentdu.onStatus(callback),
      (runtimeStatus) => {
        if (cancelled) return;
        setStatus(runtimeStatus);
        if (runtimeStatus.state === "error") {
          setNotice(runtimeStatus.error ?? "Python Runtime 启动失败。");
          setTaskStarting(false);
          dispatchTaskLifecycle({ type: "runtime-lost" });
          setRuntimeCheckStatus((current) =>
            ["starting", "running", "cancelling"].includes(current) ? "failed" : current,
          );
        }
      },
      () => void loadRecentWorkspaces(),
      (error) => {
        if (!cancelled) setNotice(error instanceof Error ? error.message : "AgentDU 初始化失败。");
      },
    );

    return () => {
      cancelled = true;
      stopStatus();
    };
  }, []);

  useEffect(() => {
    const stopEvent = window.agentdu.onEvent((event) => {
      const payload = event.payload;
      const activity: Activity = {
        id: event.eventId,
        sequence: event.sequence,
        event: event.event,
        occurredAt: typeof payload.occurred_at === "string" ? payload.occurred_at : new Date().toISOString(),
        payload,
      };
      setActivities((items) => [...items, activity].slice(-500));
      if (event.event === "task.accepted" && typeof payload.taskId === "string") {
        dispatchTaskLifecycle({ type: "accepted", taskId: payload.taskId });
        if (payload.source === "controlled-test") {
          runtimeCheckTaskIdRef.current = payload.taskId;
          runtimeCheckSessionIdRef.current =
            typeof payload.sessionId === "string" ? payload.sessionId : null;
          setRuntimeCheckTaskId(payload.taskId);
          setRuntimeCheckStatus("running");
        }
      }
      if (event.event === "assistant_delta") {
        if (
          payload.source === "controlled-test" &&
          payload.sessionId === runtimeCheckSessionIdRef.current
        ) {
          setRuntimeCheckOutput((text) => text + String(payload.text ?? ""));
        } else if (payload.sessionId === sessionId) {
          setStreamingText((text) => text + String(payload.text ?? ""));
        }
      }
      if (event.event === "task.cancel_requested" && payload.taskId === runtimeCheckTaskIdRef.current) {
        setRuntimeCheckStatus("cancelling");
      }
      if (event.event === "permission.requested") {
        setPermission({
          approvalId: String(payload.approvalId ?? ""),
          taskId: String(payload.taskId ?? ""),
          toolName: String(payload.toolName ?? "文件写入"),
          permission: String(payload.permission ?? "write"),
          arguments: (payload.arguments as Record<string, unknown>) ?? {},
          workspace: String(payload.workspace ?? ""),
          operation: String(payload.operation ?? "项目文件操作"),
          operationId: typeof payload.operationId === "string" ? payload.operationId : undefined,
          command: typeof payload.command === "string" ? payload.command : undefined,
          targetFiles: Array.isArray(payload.targetFiles) ? payload.targetFiles.map(String) : [],
          scope: String(payload.scope ?? "当前项目"),
          networkRequired: payload.networkRequired === true,
        });
      }
      if (event.event === "permission.resolved" && payload.approvalId === permission?.approvalId) setPermission(null);
      if (["run_completed", "task.completed", "task.cancelled", "task.failed"].includes(event.event)) {
        const taskId = typeof payload.taskId === "string" ? payload.taskId : null;
        if (taskId) {
          dispatchTaskLifecycle({ type: "finished", taskId });
          setTaskStarting(false);
        }
        if (payload.source === "controlled-test" && taskId === runtimeCheckTaskIdRef.current) {
          setRuntimeCheckStatus(
            event.event === "task.cancelled"
              ? "cancelled"
              : event.event === "task.failed"
                ? "failed"
                : "completed",
          );
        }
        if (payload.source === "provider") {
          setStreamingText("");
          if (sessionId && payload.sessionId === sessionId) void restoreSession(sessionId);
          if (project && payload.sessionId === sessionId) void refreshProject();
        }
      }
      if (event.event === "workspace.write.finished" && payload.operationId === pendingFileWriteRef.current) {
        setPendingFileWriteId(null);
        if (payload.status === "completed") {
          setFileContent(fileDraftRef.current);
          setEditing(false);
          if (selectedFile) void loadFile(selectedFile);
          void refreshProject();
          void loadGitDiff();
        } else if (payload.status === "denied") {
          setNotice("文件写入审批已拒绝，草稿仍保留在编辑器中。");
        } else {
          setNotice(`文件写入未完成，状态为 ${String(payload.status ?? "未知")}。草稿仍保留在编辑器中。`);
        }
      }
      if (event.event === "terminal.command.finished") {
        setTerminalBusy(false);
        if (sessionId) void restoreSession(sessionId);
      }
    });

    return () => {
      stopEvent();
    };
  }, [permission?.approvalId, sessionId, project?.path, selectedFile]);

  async function loadProject(path: string) {
    const data = await window.agentdu.inspectWorkspace();
    setProject(data);
    setSelectedFile(null);
    setFileContent("");
    setFileDraft("");
    setEditing(false);
    const recent = await window.agentdu.listWorkspaces();
    setProjects(recent);
    const listed = await window.agentdu.listSessions();
    setSessions(listed);
    if (listed.length > 0) await restoreSession(listed[0].session_id);
    else await createSession();
    setNotice(null);
    void path;
  }

  async function selectRecentProject(workspace: WorkspaceEntry) {
    setNotice(null);
    try {
      const path = await window.agentdu.openRecentWorkspace(workspace.path);
      if (path) await loadProject(path);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "无法恢复此项目。");
    }
  }

  async function chooseProject() {
    setNotice(null);
    try {
      const path = await window.agentdu.openWorkspace();
      if (path) await loadProject(path);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "无法打开所选项目。");
    }
  }

  async function refreshProject() {
    if (!project) return;
    try {
      const [details, recent, listed] = await Promise.all([
        window.agentdu.inspectWorkspace(),
        window.agentdu.listWorkspaces(),
        window.agentdu.listSessions(),
      ]);
      setProject(details);
      setProjects(recent);
      setSessions(listed);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "刷新项目状态失败。");
    }
  }

  async function createSession() {
    try {
      const id = await window.agentdu.createSession();
      setSessions(await window.agentdu.listSessions());
      await restoreSession(id);
      setNotice(null);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "无法新建会话。");
    }
  }

  async function restoreSession(id: string) {
    try {
      const record = await window.agentdu.readSession(id);
      setSessionId(record.sessionId);
      setMessages(record.messages);
      setActivities(record.events.map(eventToActivity));
      const terminalOperations = new Map<string, TerminalRecord>();
      for (const event of record.events) {
        const payload = event.payload;
        if (typeof payload.operationId !== "string") continue;
        const recordForCommand = terminalOperations.get(payload.operationId) ?? {
          operationId: payload.operationId,
          command: "",
          workingDirectory: "",
          status: "pending",
          exitCode: null,
          output: "",
        };
        if (event.kind === "terminal.command.requested") {
          recordForCommand.command = typeof payload.command === "string" ? payload.command : "";
          recordForCommand.workingDirectory = typeof payload.workingDirectory === "string" ? payload.workingDirectory : "";
        }
        if (event.kind === "terminal.command.result") {
          recordForCommand.status = typeof payload.status === "string" ? payload.status : "unknown";
          recordForCommand.exitCode = typeof payload.exitCode === "number" ? payload.exitCode : null;
          recordForCommand.output = typeof payload.output === "string" ? payload.output : "";
        }
        terminalOperations.set(payload.operationId, recordForCommand);
      }
      setTerminalHistory([...terminalOperations.values()].slice(-20).reverse());
      setStreamingText("");
      setSessions(await window.agentdu.listSessions());
      setNotice(null);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "无法恢复该会话。");
    }
  }

  async function loadFile(path: string) {
    try {
      const file = await window.agentdu.readFile(path);
      setSelectedFile(file.path);
      setFileContent(file.content);
      setFileDraft(file.content);
      setEditing(false);
      setInspectorTab("files");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "读取文件失败。");
    }
  }

  async function saveFile() {
    if (!selectedFile || workspaceBusy) return;
    setNotice(null);
    try {
      if (!sessionId) throw new Error("请先创建或恢复会话，再保存文件。");
      const result = await window.agentdu.writeFile(selectedFile, fileDraft, sessionId);
      setPendingFileWriteId(result.operationId ?? null);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "文件写入失败。");
    }
  }

  async function loadGitDiff(staged = stagedDiff) {
    try {
      setGitDiff(await window.agentdu.getGitDiff(undefined, staged));
    } catch (error) {
      setGitDiff(error instanceof Error ? error.message : "读取 Git Diff 失败。");
    }
  }

  async function startRuntimeCheck() {
    if (status.state !== "ready" || busy) return;
    setNotice(null);
    setRuntimeCheckStatus("starting");
    setRuntimeCheckTaskId(null);
    setRuntimeCheckOutput("");
    runtimeCheckTaskIdRef.current = null;
    runtimeCheckSessionIdRef.current = null;
    setTaskStarting(true);
    try {
      const task = await window.agentdu.startTestTask();
      dispatchTaskLifecycle({ type: "request-resolved", taskId: task.taskId });
      setTaskStarting(false);
    } catch (error) {
      setTaskStarting(false);
      if (runtimeCheckTaskIdRef.current) {
        setRuntimeCheckStatus("running");
        setNotice("Runtime 已接收自检任务，但启动响应未能确认。请查看任务事件。");
      } else {
        setRuntimeCheckStatus("failed");
        setNotice(error instanceof Error ? error.message : "无法启动 Runtime 自检。");
      }
    }
  }

  async function sendPrompt() {
    if (!project || !sessionId || !prompt.trim() || busy) return;
    setNotice(null);
    setTaskStarting(true);
    setStreamingText("");
    const submitted = prompt.trim();
    setPrompt("");
    try {
      const task = await window.agentdu.startTask(submitted, sessionId, selectedProfile || currentProfile?.name);
      dispatchTaskLifecycle({ type: "request-resolved", taskId: task.taskId });
      setTaskStarting(false);
      setMessages((items) => [...items, { role: "user", content: submitted }]);
    } catch (error) {
      setTaskStarting(false);
      setNotice(error instanceof Error ? error.message : "无法启动模型任务。");
    }
  }

  async function cancelTask() {
    if (!activeTaskId) return;
    if (runtimeCheckTaskIdRef.current === activeTaskId) setRuntimeCheckStatus("cancelling");
    try {
      const result = await window.agentdu.cancelTask(activeTaskId);
      dispatchTaskLifecycle({ type: "finished", taskId: activeTaskId });
      if (runtimeCheckTaskIdRef.current === activeTaskId) {
        setRuntimeCheckStatus(
          result.status === "cancelled"
            ? "cancelled"
            : result.status === "not_found"
              ? "failed"
              : "completed",
        );
      }
    } catch (error) {
      if (runtimeCheckTaskIdRef.current === activeTaskId) setRuntimeCheckStatus("running");
      setNotice(error instanceof Error ? error.message : "取消请求失败。任务仍在运行，可以重试停止。");
    }
  }

  async function runTerminalCommand() {
    if (!sessionId || !terminalCommand.trim() || terminalBusy || busy || pendingFileWriteId) return;
    setTerminalBusy(true);
    setNotice(null);
    try {
      await window.agentdu.runCommand(terminalCommand.trim(), sessionId);
      setTerminalCommand("");
    } catch (error) {
      setTerminalBusy(false);
      setNotice(error instanceof Error ? error.message : "无法提交终端命令。");
    }
  }

  async function resolvePermission(decision: "allow" | "deny") {
    if (!permission) return;
    try {
      await window.agentdu.respondToPermission(permission.approvalId, decision);
      setPermission(null);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "审批回复失败。");
    }
  }

  async function saveProvider() {
    if (!providerName.trim() || !providerModel.trim() || providerBusy) return;
    setProviderBusy(true);
    setProviderTest(null);
    try {
      const profile: ProviderProfileInput = {
        name: providerName.trim(),
        baseUrl: providerBaseUrl.trim(),
        model: providerModel.trim(),
        wireApi: providerWireApi,
        apiKey: providerKey,
        default: makeDefault,
      };
      const updated = await window.agentdu.saveProviderProfile(profile);
      setStatus((current) => ({ ...current, providerProfiles: updated, providerConfigured: Boolean(updated?.length) }));
      setSelectedProfile(providerName.trim());
      setProviderKey("");
      setProviderTest("已保存。API Key 使用系统加密保存，Runtime 已重新载入配置。");
    } catch (error) {
      setProviderTest(error instanceof Error ? error.message : "保存 Provider 配置失败。");
    } finally {
      setProviderBusy(false);
    }
  }

  async function testProvider() {
    if (!currentProfile) return;
    setProviderBusy(true);
    setProviderTest(null);
    try {
      const result = await window.agentdu.testProvider(currentProfile.name);
      setProviderTest(result.reachable ? `连接成功，HTTP ${result.statusCode}。已验证 /models 端点可访问，模型工具能力尚未验证。` : `连接失败${result.statusCode ? `，HTTP ${result.statusCode}` : ""}。`);
    } catch (error) {
      setProviderTest(error instanceof Error ? error.message : "Provider 连通性测试失败。");
    } finally {
      setProviderBusy(false);
    }
  }

  async function deleteProvider() {
    if (!providerName || providerBusy || !profiles.some((profile) => profile.name === providerName)) return;
    if (!window.confirm(`删除 Provider 档案“${providerName}”及其加密凭据？`)) return;
    setProviderBusy(true);
    setProviderTest(null);
    try {
      const updated = await window.agentdu.deleteProviderProfile(providerName);
      setStatus((current) => ({ ...current, providerProfiles: updated, providerConfigured: Boolean(updated?.some((profile) => profile.default)) }));
      setSelectedProfile(updated?.find((profile) => profile.default)?.name ?? updated?.[0]?.name ?? "");
      setProviderName("");
      setProviderModel("");
      setProviderKey("");
      setProviderTest("档案和已保存凭据已删除。");
    } catch (error) {
      setProviderTest(error instanceof Error ? error.message : "删除 Provider 档案失败。");
    } finally {
      setProviderBusy(false);
    }
  }

  const controlledTestActive =
    runtimeCheckStatus === "starting" ||
    runtimeCheckStatus === "running" ||
    runtimeCheckStatus === "cancelling";
  const runtimeCheckActivities = activities
    .filter(
      (activity) =>
        activity.payload.source === "controlled-test" &&
        activity.payload.taskId === runtimeCheckTaskId,
    )
    .slice(-12);
  const visibleFiles = useMemo(() => {
    const query = fileQuery.trim().toLocaleLowerCase();
    return project?.files.filter((file) => !query || file.path.toLocaleLowerCase().includes(query)) ?? [];
  }, [project?.files, fileQuery]);

  return (
    <div className="workbench">
      <aside className="workbench-sidebar">
        <div className="brand-row"><div className="brand-mark">A</div><div><div className="brand-name">AgentDU</div><div className="brand-caption">DESKTOP · D2</div></div></div>
        <div className="sidebar-section-heading"><span>项目</span><button type="button" onClick={() => void chooseProject()} title="打开项目">＋</button></div>
        <div className="project-list">
          {projects.map((item) => (
            <button className={`project-nav ${project?.path === item.path ? "selected" : ""}`} key={item.path} type="button" disabled={workspaceBusy} onClick={() => void selectRecentProject(item)} title={item.path}>
              <span className="nav-symbol">▱</span><span className="project-nav-name">{item.name}</span>
            </button>
          ))}
          {projects.length === 0 && <button className="empty-project" type="button" onClick={() => void chooseProject()}>＋ 选择本地项目</button>}
        </div>
        {project && <>
          <div className="sidebar-section-heading session-heading"><span>会话</span><button type="button" disabled={workspaceBusy} onClick={() => void createSession()} title="新建会话">＋</button></div>
          <div className="session-list">
            {sessions.map((session) => <button key={session.session_id} type="button" disabled={workspaceBusy} className={`session-nav ${sessionId === session.session_id ? "selected" : ""}`} onClick={() => void restoreSession(session.session_id)}>
              <span className="session-nav-title">{session.last_prompt || "新会话"}</span><span className="session-nav-meta">{session.modified_at ? formatTime(session.modified_at) : ""}</span>
            </button>)}
          </div>
        </>}
        <div className="sidebar-bottom"><div className="runtime-mini"><span className={`tiny-dot ${status.state}`} /><span>本地 Runtime</span><span className="runtime-mini-state">{status.state === "ready" ? "在线" : status.state === "error" ? "异常" : "启动中"}</span></div>
          <button className="provider-open" type="button" onClick={() => setShowProviderSettings(true)}>模型与 Provider</button>
        </div>
      </aside>

      <main className="chat-column">
        <header className="workbench-topbar">
          <div className="breadcrumbs"><span>{project?.name ?? "AgentDU"}</span><i>/</i><strong>{sessionId ? "编程会话" : "工作区"}</strong></div>
          <div className="topbar-right"><span className={`status-pill ${status.state}`}><span className="tiny-dot" />{status.state === "ready" ? "Runtime 已连接" : status.state === "error" ? "Runtime 异常" : "Runtime 启动中"}</span>
            <button className="icon-button" type="button" title="刷新项目、会话和 Git 状态" onClick={() => void refreshProject()}>↻</button><button className="secondary-button inspector-mobile" type="button" onClick={() => setInspectorVisible((visible) => !visible)}>{inspectorVisible ? "收起文件" : "文件 / Diff"}</button><button className="secondary-button provider-mobile" type="button" onClick={() => setShowProviderSettings(true)}>模型</button>
          </div>
        </header>
        {status.recoveredAfterCrash && <div className="recovery-banner"><strong>检测到上次异常关闭</strong><span>未完成操作没有自动重放。请检查会话与项目状态后再继续。</span></div>}
        {notice && <div className="notice-banner" role="alert">{notice}<button type="button" onClick={() => setNotice(null)}>关闭</button></div>}
        {!project ? <div className="welcome-state">
          <div className="welcome-mark">A</div>
          <h1>开始一个编程任务</h1>
          <p>打开本地项目，查看代码、Git 变更并恢复项目会话。</p>
          <div className="welcome-actions">
            <button className="primary-button" type="button" disabled={busy} onClick={() => void chooseProject()}>打开本地项目</button>
            {controlledTestActive ? (
              <button
                className="secondary-button"
                type="button"
                disabled={!activeTaskId || runtimeCheckStatus === "cancelling"}
                onClick={() => void cancelTask()}
              >
                {activeTaskId ? "停止 Runtime 自检" : "自检启动中…"}
              </button>
            ) : (
              <button
                className="secondary-button"
                type="button"
                disabled={busy || status.state !== "ready"}
                onClick={() => void startRuntimeCheck()}
              >
                {runtimeCheckStatus === "idle" ? "运行 Runtime 自检" : "重新运行 Runtime 自检"}
              </button>
            )}
          </div>
          {runtimeCheckStatus !== "idle" && (
            <section className="runtime-check-panel" aria-live="polite">
              <div className="runtime-check-heading">
                <strong>Runtime 自检</strong>
                <span>{RUNTIME_CHECK_STATUS_LABELS[runtimeCheckStatus]}</span>
              </div>
              <p>使用本地受控模型和隔离临时目录，不请求外部 Provider，也不修改项目文件。</p>
              {runtimeCheckOutput ? (
                <pre className="runtime-check-output">{runtimeCheckOutput}</pre>
              ) : (
                <div className="runtime-check-output empty">等待 Runtime 流式事件…</div>
              )}
              {runtimeCheckActivities.length > 0 && (
                <div className="runtime-check-events" aria-label="Runtime 自检事件">
                  {runtimeCheckActivities.map((activity) => (
                    <div className="runtime-check-event" key={activity.id}>
                      <span>{EVENT_LABELS[activity.event] ?? activity.event}</span>
                      <span>{describeActivity(activity)}</span>
                    </div>
                  ))}
                </div>
              )}
            </section>
          )}
        </div> : <>
          <div className="chat-project-strip"><div><strong>{project.name}</strong><span title={project.path}>{project.path}</span></div><span className={`git-branch ${project.gitError ? "error" : ""}`}>{project.gitError ? "Git 不可用" : project.gitStatus.split("\n")[0] || "Git 工作区"}</span></div>
          <section className="conversation" aria-label="会话消息">
            {messages.filter((message) => message.role !== "system").length === 0 && !streamingText ? <div className="conversation-empty"><div className="empty-timeline">✳</div><strong>你想在这个项目里完成什么？</strong><span>AgentDU 会读取真实项目文件，并通过审批执行写入操作。</span></div> : messages.filter((message) => message.role !== "system").map((message, index) => {
              const role = String(message.role ?? "message");
              const text = messageText(message);
              return <article className={`chat-message ${role}`} key={`${sessionId}-${index}`}>
                <div className="message-avatar">{role === "user" ? "你" : role === "tool" ? "T" : "A"}</div>
                <div className="message-body"><div className="message-role">{role === "user" ? "你" : role === "tool" ? `工具结果 · ${String(message.name ?? "")}` : "AgentDU"}</div>
                  {text ? <pre>{text}</pre> : role === "assistant" && Array.isArray(message.tool_calls) ? <div className="message-tool-call">请求工具操作 · 可在下方活动记录中查看</div> : null}
                </div>
              </article>;
            })}
            {streamingText && <article className="chat-message assistant"><div className="message-avatar">A</div><div className="message-body"><div className="message-role">AgentDU · 正在回复</div><pre>{streamingText}</pre></div></article>}
            <div className="activity-panel"><div className="activity-heading"><strong>Agent 活动</strong><span>{activities.length} 条持久与实时事件</span></div>
              {activities.length === 0 ? <div className="activity-empty">开始任务后，这里会显示模型请求、工具调用、审批和运行结果。</div> : <div className="activity-list" role="log" aria-live="polite">
                {activities.slice(-60).map((activity) => <div className="activity-row" key={activity.id}><time>{formatTime(activity.occurredAt)}</time><span className={`activity-dot ${activity.event.includes("failed") ? "failed" : ""}`} /><strong>{EVENT_LABELS[activity.event] ?? activity.event}</strong><span className="activity-detail">{describeActivity(activity)}</span></div>)}
              </div>}
            </div>
          </section>
          <div className="composer-wrap">
            <div className="composer-meta"><label>模型<select value={currentProfile?.name ?? ""} onChange={(event) => setSelectedProfile(event.target.value)} disabled={profiles.length === 0}>
              {profiles.length === 0 && <option value="">尚未配置 Provider</option>}{profiles.map((profile) => <option key={profile.name} value={profile.name}>{profile.name} · {profile.model}</option>)}
            </select></label><span>{currentProfile ? `${currentProfile.model} · ${currentProfile.protocol ?? "协议未确认"} · ${currentProfile.streaming ? "流式已配置" : "未配置流式"}` : "添加 Provider 后开始对话"}</span><button type="button" className="text-button" onClick={() => setShowProviderSettings(true)}>配置</button></div>
            <div className="composer"><textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendPrompt(); } }} placeholder="描述你希望完成的代码任务…" rows={3} maxLength={16000} disabled={!sessionId} />
              <div className="composer-actions"><span>Enter 发送 · Shift+Enter 换行</span>{busy ? <button className="secondary-button" type="button" disabled={!activeTaskId} onClick={() => void cancelTask()}>{activeTaskId ? "停止" : "启动中…"}</button> : <button className="primary-button" type="button" disabled={!sessionId || !currentProfile?.credentialAvailable && !isLocalProvider(currentProfile?.baseUrl) || !prompt.trim()} onClick={() => void sendPrompt()}>发送任务 ↑</button>}</div>
            </div>
            {!currentProfile && <div className="composer-warning">请配置 Provider 档案和凭据。模型能力会在实际请求中验证。</div>}
          </div>
        </>}
      </main>

      <aside className={`inspector ${inspectorVisible ? "visible" : ""}`}>
        <div className="inspector-tabs"><button type="button" className={inspectorTab === "files" ? "active" : ""} onClick={() => setInspectorTab("files")}>文件</button><button type="button" className={inspectorTab === "diff" ? "active" : ""} onClick={() => { setInspectorTab("diff"); void loadGitDiff(); }}>Git Diff</button><button type="button" className={inspectorTab === "terminal" ? "active" : ""} onClick={() => setInspectorTab("terminal")}>终端</button></div>
        {inspectorTab === "files" && <div className="inspector-content"><div className="inspector-heading"><strong>{selectedFile ? "文件查看" : "项目文件"}</strong><span>{project?.name ?? "未打开项目"}</span></div>
          {selectedFile ? <><div className="file-toolbar"><button className="text-button" type="button" disabled={Boolean(pendingFileWriteId) || workspaceBusy} onClick={() => { setSelectedFile(null); setEditing(false); }}>‹ 返回文件列表</button><span className="file-path" title={selectedFile}>{selectedFile}</span></div><div className="editor-toolbar"><span>{pendingFileWriteId ? "等待写入审批" : workspaceBusy ? "任务运行中，编辑已锁定" : editing ? "待审批保存" : "只读查看"}</span><div>{editing ? <><button className="text-button" type="button" disabled={workspaceBusy} onClick={() => { setFileDraft(fileContent); setEditing(false); }}>撤销</button><button className="primary-button compact" type="button" disabled={!fileDirty || workspaceBusy} onClick={() => void saveFile()}>保存</button></> : <button className="secondary-button compact" type="button" disabled={workspaceBusy} onClick={() => setEditing(true)}>编辑</button>}</div></div><textarea className="code-editor" value={editing ? fileDraft : fileContent} onChange={(event) => setFileDraft(event.target.value)} readOnly={!editing || workspaceBusy} spellCheck={false} /></> : <>
            <input className="file-search" value={fileQuery} onChange={(event) => setFileQuery(event.target.value)} placeholder="筛选文件…" />
            <div className="file-list">{visibleFiles.map((file) => <button className="file-row" key={file.path} type="button" onClick={() => void loadFile(file.path)}><span>{file.path.split("/").slice(0, -1).join("/")}</span><strong>{file.path.split("/").at(-1)}</strong></button>)}{visibleFiles.length === 0 && <div className="inspector-empty">项目中没有匹配的文本文件。</div>}</div>
            <div className="git-summary"><div><strong>Git 状态</strong><button type="button" className="text-button" onClick={() => void refreshProject()}>刷新</button></div><pre>{project?.gitError ?? project?.gitStatus ?? ""}</pre></div>
          </>}
        </div>}
        {inspectorTab === "diff" && <div className="inspector-content"><div className="inspector-heading"><strong>Git Diff</strong><button type="button" className="text-button" onClick={() => void loadGitDiff()}>刷新</button></div><label className="staged-toggle"><input type="checkbox" checked={stagedDiff} onChange={(event) => { setStagedDiff(event.target.checked); void loadGitDiff(event.target.checked); }} />查看暂存区</label><pre className="diff-view">{gitDiff || "当前没有差异。"}</pre></div>}
        {inspectorTab === "terminal" && <div className="inspector-content"><div className="inspector-heading"><strong>隔离终端</strong><span>AppContainer · 无网络</span></div><div className="terminal-composer"><textarea value={terminalCommand} onChange={(event) => setTerminalCommand(event.target.value)} placeholder="输入要执行的命令…" rows={3} maxLength={16000} disabled={terminalBusy || busy} /><button className="primary-button" type="button" disabled={!sessionId || !terminalCommand.trim() || terminalBusy || busy} onClick={() => void runTerminalCommand()}>{terminalBusy ? "等待审批或运行中…" : "请求执行"}</button></div><div className="terminal-help">每条命令都要单独审批。命令在当前项目的 Windows AppContainer 中执行，网络访问关闭，并限制进程数、内存、输出和最长运行时间。</div><div className="terminal-history">{terminalHistory.map((record) => <article className="terminal-record" key={record.operationId}><div className="terminal-record-meta"><span>{record.status === "completed" ? `退出码 ${record.exitCode ?? "未知"}` : record.status === "denied" ? "已拒绝" : record.status}</span><time>{record.workingDirectory}</time></div><code>{record.command}</code><pre>{record.output || "（无输出）"}</pre></article>)}{terminalHistory.length === 0 && <div className="inspector-empty">命令申请、审批决定、执行结果和退出码会记录在当前会话中。</div>}</div></div>}
      </aside>

      {permission && <div className="approval-backdrop"><section className="approval-dialog" role="alertdialog" aria-modal="true" aria-label="操作审批"><div className="approval-title"><span className="permission-mark">!</span><div><strong>AgentDU 请求操作审批</strong><span>{permission.operation}</span></div></div>
        <dl><dt>操作来源</dt><dd>{permission.toolName}</dd><dt>实际执行命令</dt><dd>{permission.command ?? "文件写入请求"}</dd><dt>工作目录</dt><dd>{permission.workspace}</dd><dt>目标文件</dt><dd>{permission.targetFiles?.join("、") || String(permission.arguments.path ?? "未提供")}</dd><dt>操作范围</dt><dd>{permission.scope}</dd><dt>网络需求</dt><dd>{permission.networkRequired ? "需要网络" : "不需要"}</dd><dt>变更内容</dt><dd>{typeof permission.arguments.contentChars === "number" ? `写入 ${permission.arguments.contentChars} 个字符` : "按实际命令执行"}</dd></dl>
        <p className="approval-note">授权请求和实际执行结果分别记录在活动时间线中。</p><div className="approval-actions"><button className="secondary-button" type="button" onClick={() => void resolvePermission("deny")}>拒绝</button><button className="primary-button" type="button" onClick={() => void resolvePermission("allow")}>允许本次操作</button></div></section></div>}

      {showProviderSettings && <div className="modal-backdrop" role="presentation"><section className="provider-modal" role="dialog" aria-modal="true" aria-labelledby="provider-title"><header><div><div className="panel-kicker">MODEL PROFILES</div><h2 id="provider-title">模型与 Provider</h2></div><button className="icon-button" type="button" onClick={() => setShowProviderSettings(false)} aria-label="关闭">×</button></header>
        <div className="provider-profile-list">{profiles.map((profile) => <button type="button" className={`provider-profile-row ${providerName === profile.name ? "selected" : ""}`} key={profile.name} onClick={() => { setProviderName(profile.name); setSelectedProfile(profile.name); setProviderBaseUrl(profile.baseUrl ?? "https://api.openai.com/v1"); setProviderModel(profile.model ?? ""); setProviderWireApi(profile.wireApi ?? "chat_completions"); setMakeDefault(profile.default); setProviderTest(null); }}><span><strong>{profile.name}</strong><small>{profile.baseUrl ?? "配置文件 Provider"} · {profile.model}</small></span><span>{profile.credentialAvailable ? "凭据可用" : "无凭据"}{profile.default ? " · 默认" : ""}</span></button>)}</div>
        <div className="provider-form"><label>档案名称<input value={providerName} onChange={(event) => setProviderName(event.target.value)} placeholder="例如 openai-main" /></label><label>Endpoint<input value={providerBaseUrl} onChange={(event) => setProviderBaseUrl(event.target.value)} placeholder="https://api.example.com/v1" /></label><label>Model ID<input value={providerModel} onChange={(event) => setProviderModel(event.target.value)} placeholder="模型 ID" /></label><label>API 协议<select value={providerWireApi} onChange={(event) => setProviderWireApi(event.target.value as "chat_completions" | "responses")}><option value="chat_completions">Chat Completions</option><option value="responses">Responses API</option></select></label><label>API Key<input type="password" autoComplete="new-password" value={providerKey} onChange={(event) => setProviderKey(event.target.value)} placeholder={profiles.find((item) => item.name === providerName)?.credentialAvailable ? "已保存凭据，留空则保留" : "输入 API Key"} /></label><label className="default-profile"><input type="checkbox" checked={makeDefault} onChange={(event) => setMakeDefault(event.target.checked)} />设为默认模型档案</label>
          {profiles.find((profile) => profile.name === providerName) && <div className="provider-capability-note"><div>协议：{profiles.find((profile) => profile.name === providerName)?.protocol ?? "未确认"}</div><div>流式输出：{profiles.find((profile) => profile.name === providerName)?.streaming ? "已配置" : "未配置"}</div><div>配置声明能力：{profiles.find((profile) => profile.name === providerName)?.configuredCapabilities?.join("、") || "未声明"}</div></div>}<div className="provider-capability-note">连通性测试只验证 /models 响应。配置声明不代表模型已具备工具调用等能力，实际能力仍需通过真实任务验证。</div><div className="provider-actions">{providerName && profiles.some((profile) => profile.name === providerName) && <button className="secondary-button" type="button" disabled={providerBusy} onClick={() => void deleteProvider()}>删除档案</button>}<button className="secondary-button" type="button" disabled={!currentProfile || providerBusy} onClick={() => void testProvider()}>测试连通性</button><button className="primary-button" type="button" disabled={providerBusy || !providerName.trim() || !providerModel.trim()} onClick={() => void saveProvider()}>{providerBusy ? "处理中…" : "安全保存"}</button></div>{providerTest && <div className="provider-result" role="status">{providerTest}</div>}
        </div></section></div>}
    </div>
  );
}

function isLocalProvider(baseUrl: string | undefined): boolean {
  if (!baseUrl) return false;
  try { return ["localhost", "127.0.0.1", "::1"].includes(new URL(baseUrl).hostname); } catch { return false; }
}
