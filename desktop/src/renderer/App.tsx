import { useEffect, useMemo, useState } from "react";
import type { PermissionRequestEvent, RuntimeStatus } from "../shared/protocol";

type Activity = {
  id: string;
  sequence: number;
  event: string;
  occurredAt: string;
  payload: Record<string, unknown>;
};

type Workspace = { path: string; name: string } | null;

const EVENT_LABELS: Record<string, string> = {
  "host.ready": "Runtime 握手完成",
  "host.health": "健康检查",
  "workspace.opened": "工作区已打开",
  "task.accepted": "任务已接收",
  run_started: "Agent 运行开始",
  model_request: "正在请求模型",
  assistant_delta: "收到流式文本",
  assistant_message: "模型消息已保存",
  model_usage: "用量已记录",
  run_completed: "Agent 运行完成",
  run_stopped: "Agent 运行停止",
  tool_started: "工具开始执行",
  tool_finished: "工具执行结束",
  "permission.requested": "等待文件操作审批",
  "permission.resolved": "文件操作审批已处理",
  "task.cancel_requested": "已请求取消任务",
  "task.cancelled": "任务已取消",
  "task.failed": "任务运行失败",
  "protocol.error": "协议错误",
};

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function describeEvent(activity: Activity): string {
  const { event, payload } = activity;
  if (event === "assistant_delta") return String(payload.text ?? "收到文本片段");
  if (event === "task.accepted") {
    return payload.source === "controlled-test"
      ? "受控流式测试已启动，不会调用模型服务。"
      : `会话 ${String(payload.sessionId ?? "")} 已开始。`;
  }
  if (event === "model_request") return `第 ${String(payload.step ?? "")} 轮模型请求`;
  if (event === "tool_started" || event === "tool_finished") {
    return String(payload.tool_name ?? "工具");
  }
  if (event === "task.failed") return String(payload.message ?? "任务失败");
  if (event === "protocol.error") return String(payload.message ?? "Runtime 返回了无效 IPC 消息。");
  if (event === "permission.requested") return `需要批准 ${String(payload.toolName ?? "文件写入")}`;
  if (event === "run_completed") return "Runtime 已收到模型的最终响应。";
  if (event === "task.cancelled") return "Runtime 确认执行任务已取消。";
  if (payload.error) return String(payload.error);
  return String(payload.message ?? payload.status ?? "状态已更新");
}

export function App() {
  const [status, setStatus] = useState<RuntimeStatus>({ state: "starting" });
  const [workspace, setWorkspace] = useState<Workspace>(null);
  const [activities, setActivities] = useState<Activity[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [permission, setPermission] = useState<PermissionRequestEvent | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const profileSummary = useMemo(() => {
    const profiles = status.providerProfiles ?? [];
    if (profiles.length === 0) return "未配置模型档案";
    const selected = profiles.find((profile) => profile.default) ?? profiles[0];
    return `${selected.name} · ${selected.model ?? "未指定模型"}`;
  }, [status.providerProfiles]);

  const selectedProfile = useMemo(() => {
    const profiles = status.providerProfiles ?? [];
    return profiles.find((profile) => profile.default) ?? profiles[0] ?? null;
  }, [status.providerProfiles]);

  useEffect(() => {
    void window.agentdu.getStatus().then(setStatus).catch((error: unknown) => {
      setNotice(error instanceof Error ? error.message : "读取 Runtime 状态失败。");
    });
    const offEvent = window.agentdu.onEvent((event) => {
      const payload = event.payload;
      const activity: Activity = {
        id: event.eventId,
        sequence: event.sequence,
        event: event.event,
        occurredAt: typeof payload.occurred_at === "string" ? payload.occurred_at : new Date().toISOString(),
        payload,
      };
      setActivities((items) => [activity, ...items].slice(0, 160));
      if (event.event === "permission.requested") {
        setPermission({
          approvalId: String(payload.approvalId ?? ""),
          taskId: String(payload.taskId ?? ""),
          toolName: String(payload.toolName ?? "edit_file"),
          permission: String(payload.permission ?? "write"),
          arguments: (payload.arguments as Record<string, unknown>) ?? {},
          workspace: String(payload.workspace ?? ""),
        });
      }
      if (event.event === "permission.resolved" && payload.approvalId === permission?.approvalId) {
        setPermission(null);
      }
      if (["task.cancelled", "run_completed", "task.failed"].includes(event.event)) {
        setActiveTaskId((current) => (payload.taskId === current ? null : current));
        setBusy(false);
      }
    });
    const offStatus = window.agentdu.onStatus(setStatus);
    return () => {
      offEvent();
      offStatus();
    };
  }, [permission?.approvalId]);

  async function chooseWorkspace() {
    setNotice(null);
    try {
      const path = await window.agentdu.openWorkspace();
      if (path) {
        const name = path.split(/[\\/]/).filter(Boolean).at(-1) ?? path;
        setWorkspace({ path, name });
      }
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "无法打开所选工作区。");
    }
  }

  async function startTest() {
    setNotice(null);
    setBusy(true);
    try {
      const task = await window.agentdu.startTestTask();
      setActiveTaskId(task.taskId);
    } catch (error) {
      setBusy(false);
      setNotice(error instanceof Error ? error.message : "无法启动受控流式测试。");
    }
  }

  async function startTask() {
    if (!workspace) {
      setNotice("请先打开一个本地项目工作区。");
      return;
    }
    if (!prompt.trim()) return;
    setNotice(null);
    setBusy(true);
    try {
      const task = await window.agentdu.startTask(prompt.trim());
      setActiveTaskId(task.taskId);
      setPrompt("");
    } catch (error) {
      setBusy(false);
      setNotice(error instanceof Error ? error.message : "无法启动模型任务。");
    }
  }

  async function cancelTask() {
    if (!activeTaskId) return;
    setNotice(null);
    try {
      const result = await window.agentdu.cancelTask(activeTaskId);
      if (result.status === "not_found") {
        setNotice("任务已结束，Runtime 中没有可取消的活动任务。");
      }
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "取消请求未能送达 Runtime。");
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

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-row">
          <div className="brand-mark">A</div>
          <div>
            <div className="brand-name">AgentDU</div>
            <div className="brand-caption">DESKTOP · D1</div>
          </div>
        </div>

        <div className="nav-label">工作区</div>
        <button className="nav-item active" type="button" aria-current="page">
          <span className="nav-symbol">◈</span>运行概览
        </button>
        <button className="nav-item" type="button" onClick={chooseWorkspace}>
          <span className="nav-symbol">⌂</span>本地项目
        </button>

        <div className="sidebar-divider" />
        <div className="nav-label">当前项目</div>
        {workspace ? (
          <div className="project-card">
            <div className="project-initial">{workspace.name.slice(0, 1).toUpperCase()}</div>
            <div className="project-copy">
              <strong>{workspace.name}</strong>
              <span title={workspace.path}>{workspace.path}</span>
            </div>
            <span className="tiny-dot ready" />
          </div>
        ) : (
          <button className="empty-project" type="button" onClick={chooseWorkspace}>
            <span>＋</span>
            <span>选择一个本地项目</span>
          </button>
        )}

        <div className="sidebar-bottom">
          <div className="sidebar-divider" />
          <div className="runtime-mini">
            <span className={`tiny-dot ${status.state}`} />
            <span>本地 Runtime</span>
            <span className="runtime-mini-state">{status.state === "ready" ? "在线" : status.state === "error" ? "异常" : "启动中"}</span>
          </div>
          <div className="user-card">
            <div className="avatar">DU</div>
            <div className="user-copy"><strong>本地开发环境</strong><span>运行数据保存在此设备</span></div>
            <button className="more-button" type="button" aria-label="更多设置">···</button>
          </div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs"><span>AgentDU</span><i>/</i><strong>运行概览</strong></div>
          <div className="topbar-right">
            <span className={`status-pill ${status.state}`}><span className="tiny-dot" />{status.state === "ready" ? "Runtime 已连接" : status.state === "error" ? "Runtime 异常" : status.state === "stopped" ? "Runtime 已停止" : "正在启动 Runtime"}</span>
            <button className="icon-button" type="button" title="重新启动 Runtime" onClick={() => void window.agentdu.restartRuntime()}>↻</button>
            <div className="topbar-divider" />
            <div className="avatar top-avatar">DU</div>
          </div>
        </header>

        <div className="page-scroll">
          <section className="page-heading">
            <div>
              <div className="eyebrow">本地 Agent 工作台</div>
              <h1>运行概览</h1>
              <p>桌面应用与 AgentDU Runtime 的本地通信状态。</p>
            </div>
            <button className="primary-button" type="button" onClick={chooseWorkspace}>
              <span>＋</span>打开项目
            </button>
          </section>

          {status.recoveredAfterCrash && (
            <section className="recovery-banner">
              <div className="recovery-icon">!</div>
              <div><strong>Runtime 上次异常退出</strong><span>之前正在运行的任务没有自动重放。请检查会话日志后再决定是否重新运行。</span></div>
              <button className="secondary-button" type="button" onClick={() => void window.agentdu.restartRuntime()}>重新启动</button>
            </section>
          )}

          {notice && <div className="notice-banner" role="alert">{notice}<button type="button" onClick={() => setNotice(null)}>关闭</button></div>}

          <section className="metrics-grid" aria-label="Runtime 状态">
            <article className="metric-card">
              <div className="metric-top"><span>Runtime 版本</span><span className="metric-icon purple">⌘</span></div>
              <div className="metric-value">{status.runtimeVersion ?? "—"}</div>
              <div className="metric-foot"><span className={`tiny-dot ${status.state}`} />{status.state === "ready" ? "Python Host 握手正常" : "等待本地 Host 握手"}</div>
            </article>
            <article className="metric-card">
              <div className="metric-top"><span>模型配置</span><span className="metric-icon blue">✳</span></div>
              <div className="metric-value metric-value-small">{profileSummary}</div>
              <div className="metric-foot">
                {status.providerConfigured
                  ? selectedProfile?.credentialAvailable
                    ? "模型配置已读取，Provider 凭据可用"
                    : "模型配置已读取，未检测到 Provider 凭据"
                  : "未配置模型档案，可运行不调用模型的 IPC 测试"}
              </div>
            </article>
            <article className="metric-card">
              <div className="metric-top"><span>桌面通信</span><span className="metric-icon green">⇄</span></div>
              <div className="metric-value">stdio JSONL</div>
              <div className="metric-foot">协议版本 1 · 双向请求与事件</div>
            </article>
            <article className="metric-card">
              <div className="metric-top"><span>活动任务</span><span className="metric-icon orange">◷</span></div>
              <div className="metric-value">{activeTaskId ? "1 个运行中" : "空闲"}</div>
              <div className="metric-foot">{activeTaskId ? `任务 ${activeTaskId.slice(0, 8)}` : "可以开始受控测试"}</div>
            </article>
          </section>

          <section className="content-grid">
            <article className="panel workspace-panel">
              <div className="panel-heading">
                <div><div className="panel-kicker">LOCAL WORKSPACE</div><h2>本地项目</h2></div>
                {workspace && <span className="connected-tag"><span className="tiny-dot ready" />已连接</span>}
              </div>
              {workspace ? (
                <div className="workspace-summary">
                  <div className="workspace-folder">▱</div>
                  <div className="workspace-info"><strong>{workspace.name}</strong><span>{workspace.path}</span></div>
                  <button className="text-button" type="button" onClick={chooseWorkspace}>切换项目</button>
                </div>
              ) : (
                <button className="workspace-drop" type="button" onClick={chooseWorkspace}>
                  <span className="folder-large">▱</span><strong>打开一个本地项目</strong><span>通过系统文件选择器授权工作目录</span><span className="secondary-button">选择文件夹</span>
                </button>
              )}
              <div className="panel-divider" />
              <div className="workspace-note"><span className="note-icon">i</span><span>Renderer 不能直接读取项目文件或启动命令。目录选择和 Runtime 工具请求由桌面主进程管理。</span></div>
            </article>

            <article className="panel test-panel">
              <div className="panel-heading">
                <div><div className="panel-kicker">IPC SMOKE TEST</div><h2>验证桌面通信</h2></div>
                <span className="fixture-tag">受控测试</span>
              </div>
              <p className="test-description">使用 AgentDU 现有 AgentRuntime 和流式事件总线，传输预设响应。此任务不连接模型，也不会修改项目文件。</p>
              <div className="test-actions">
                <button className="primary-button" type="button" disabled={status.state !== "ready" || busy} onClick={() => void startTest()}>
                  {busy ? <><span className="spinner" />测试运行中</> : <><span>▷</span>运行流式测试</>}
                </button>
                <button className="secondary-button" type="button" disabled={!activeTaskId} onClick={() => void cancelTask()}>取消任务</button>
              </div>
              <div className="safety-caption"><span className="shield-icon">◇</span>真实 Shell 和 MCP 执行在 D1 沙箱验收前保持关闭</div>
            </article>
          </section>

          <section className="panel event-panel">
            <div className="panel-heading event-heading">
              <div><div className="panel-kicker">RUNTIME EVENTS</div><h2>运行事件</h2></div>
              <div className="event-actions"><span className="live-label"><span className="pulse-dot" />实时</span><button className="text-button" type="button" onClick={() => setActivities([])}>清空</button></div>
            </div>
            {activities.length === 0 ? (
              <div className="empty-events"><div className="empty-timeline">···</div><strong>等待 Runtime 事件</strong><span>启动受控流式测试，查看真实 IPC 事件传输。</span></div>
            ) : (
              <div className="event-list" role="log" aria-live="polite">
                {activities.map((activity) => (
                  <div className={`event-row ${activity.event === "assistant_delta" ? "delta-row" : ""}`} key={activity.id}>
                    <span className="event-marker" />
                    <time>{formatTime(activity.occurredAt)}</time>
                    <span className="event-type">{EVENT_LABELS[activity.event] ?? activity.event}</span>
                    <span className="event-detail">{describeEvent(activity)}</span>
                    <span className="event-sequence">{String(activity.sequence).padStart(3, "0")}</span>
                  </div>
                ))}
              </div>
            )}
          </section>

          {workspace && (
            <section className="panel task-panel">
              <div className="panel-heading">
                <div><div className="panel-kicker">MODEL TASK · READ-ONLY D1</div><h2>提交模型任务</h2></div>
                <span className="read-only-tag">Shell 执行关闭</span>
              </div>
              <div className="task-form">
                <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} maxLength={16000} placeholder="描述一个只需要读取和分析的任务" rows={2} />
                <button className="primary-button" type="button" disabled={!status.providerConfigured || busy || !prompt.trim()} onClick={() => void startTask()}>发送任务</button>
              </div>
              {!status.providerConfigured && <div className="form-hint">先在 %APPDATA%\AgentDU\config.toml 配置模型档案。D1 不提供 API 密钥编辑或保存界面。</div>}
            </section>
          )}

          {permission && (
            <section className="permission-card" role="alertdialog" aria-label="文件操作授权">
              <div className="permission-mark">!</div>
              <div className="permission-copy"><strong>Agent 请求文件写入授权</strong><span>{permission.toolName} · {String(permission.arguments.path ?? workspace?.path ?? "")}</span></div>
              <button className="secondary-button" type="button" onClick={() => void resolvePermission("deny")}>拒绝</button>
              <button className="primary-button" type="button" onClick={() => void resolvePermission("allow")}>允许本次操作</button>
            </section>
          )}

          <footer className="page-footer"><span>AgentDU Desktop D1 · Windows x64</span><span>安全验证与运行证据见 <code>docs/desktop/d1-implementation.md</code></span></footer>
        </div>
      </main>
    </div>
  );
}
