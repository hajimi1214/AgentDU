# AgentDU Desktop D1 实施与验收记录

## 结论

D1 桌面骨架、Runtime Host、JSONL IPC、中文状态界面、Windows 安装包和 AppContainer 隔离原型已经建立并完成自动化验证。D1 **尚未通过完整运行验收**，当前标记为 **BLOCKED**。运行中的安装版已启动并完成 Python Runtime 握手。真实 Runtime 任务与取消通过 Python 子进程测试。由于 Windows 上的窗口输入被另一置顶应用拦截，无法完成安装版 UI 的任务按钮、取消按钮和正常关窗实操。AppContainer 原型没有接入 Runtime 的 Shell 执行路径，产品中的 Shell、MCP 和插件执行保持关闭。

基线为 `701d3476d75671a6ea734f5fdd5c7dcf073e47c6`。验证时工作分支为 `main`。本地最终提交 SHA 在本文末尾记录，不推送 GitHub。

## 已实现架构

```text
desktop React Renderer
  └─ 受限 window.agentdu API
      └─ Electron Preload（contextIsolation、sandbox）
          └─ Electron Main（来源校验、参数校验、进程生命周期）
              └─ Python Runtime Host（stdin/stdout JSONL，stderr 诊断日志）
                  └─ 现有 AgentRuntime、Provider、SessionStore、ToolRegistry
```

Renderer 使用 React、TypeScript、Vite 和简体中文界面。它只能调用 Preload 明确列出的状态查询、打开工作区、启动任务、取消任务、审批回复和重启 Runtime 方法。Renderer 没有 Node 集成，也没有文件系统或 Shell API。Electron 窗口启用了 `contextIsolation`、`sandbox`、`webSecurity`，关闭 Node 集成、导航、弹窗与应用权限请求。发布构建 CSP 仅允许同源连接。

Electron Main 用 `spawn(executable, args, { shell: false })` 启动 Python，参数采用数组传递。打包版使用 Electron Resources 中的固定 Python 3.14.10 embedded runtime。子进程环境通过白名单构造，不继承 Provider Key 环境变量。Python stdout 仅写 JSONL，日志写 stderr，并在主进程轮转及脱敏。

Python Host 复用现有 `AgentRuntime`，D1 受控任务使用其事件总线和真实 `AgentRuntime.run` 生命周期。受控模型固定输出文本，不发起 Provider 请求、不写项目文件。取消通过 `asyncio.Task.cancel()` 取消真实 Agent Runtime task，并等待 `task.cancelled` 事件。

## D1 JSONL 协议

协议定义在 `desktop/src/shared/protocol.ts`，Python 实现位于 `src/agentdu/desktop_host.py`。版本号为 `1`，UTF-8 每行一个 JSON 对象，最大帧为 1 MiB。请求包含 `type=request`、`protocolVersion`、`requestId`、`method` 和对象型 `params`。响应使用相同 request ID。事件包含 UUID `eventId`、单调递增 `sequence`、事件名与对象型 `payload`。

已开放方法：

| 方法 | 行为 |
| --- | --- |
| `host.initialize` | 单次初始化并发出 `host.ready`，返回 Runtime、Python 和 Provider 状态 |
| `host.health` | 返回 Runtime 和当前活动任务状态 |
| `workspace.open` | 校验绝对路径及目录后设置工作区 |
| `test.start` | 启动不访问 Provider 的流式 Runtime 测试 |
| `turn.start` | 使用现有默认模型 Profile 启动真实单模型任务 |
| `turn.cancel` | 取消任务并等待实际 Runtime task 结束 |
| `permission.respond` | 回复带超时的文件写入审批 |
| `host.shutdown` | 取消活动任务并关闭 Host |

请求 ID 内容重复时返回缓存响应，不重复启动任务。ID 被不同内容重用时返回 `REQUEST_ID_REUSED`。非法 JSON、非标准 JSON 常量、帧超限和无效消息对象发出 `protocol.error` 后以退出码 2 退出。协议版本错误、未知方法及配置问题返回结构化错误。Main 对 Runtime 子进程异常退出、启动失败、损坏 stdout 协议帧和超限帧设置可见错误状态，不自动重放副作用任务。

## Python Runtime 打包

`desktop/scripts/prepare-runtime.ps1` 从 Python.org 获取固定的 Python 3.14.10 Windows x64 embeddable ZIP，校验 SHA-256 `90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159`，将 Runtime 包复制到 embedded Python 目录，并在受限 `python314._pth` 配置下导入 `agentdu.desktop_host` 验证模块路径。Embedded Python 以 `-B` 启动，不向 Runtime 源码目录写 pyc。

版本、文件名和发布校验值来源于 [Python 3.14.10 release page](https://www.python.org/downloads/release/python-31410/)。Embedded package 的使用边界见 [Python Windows embeddable package documentation](https://docs.python.org/3.14/using/windows.html#the-embeddable-package)。

Python 选择版本兼容现有 `requires-python >=3.12`。本地完整既有测试使用 Python 3.12。D1 Host 子进程测试另用 bundled Python 3.14 执行。

## 安全验证

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| Renderer Node 集成、文件系统和任意 Shell | PASS，代码边界检查 | Renderer 只有 `window.agentdu`。Electron 配置 `nodeIntegration: false`、`sandbox: true`、`contextIsolation: true`。Preload 无通用 IPC 通道。 |
| Preload 最小权限 API | PASS，源码检查与生产构建 | API 仅包含固定 IPC 方法及事件订阅器。 |
| Provider Key 不进入命令行/子进程环境 | PASS，代码与运行配置检查 | Provider Key 未作为 spawn 参数传递，环境仅包含白名单系统变量，筛掉 `OPENAI_API_KEY` 和 `AGENTDU_API_KEY`。 |
| Python stdout/stderr 分离 | PASS，运行观察 | Host 测试通过 stdout 收到 JSONL。`runtime.stderr.log` 收到带时间戳的 Host 启动日志。 |
| 真实 Host 中拒绝 Shell execute 权限 | PASS，Host 实现检查 | `DesktopPermissionPolicy` 对 `execute` 返回拒绝并发 `security.capability_blocked`。Main 没有开放任意命令执行 IPC。 |
| AppContainer 文件读取、文件写入、网络与子进程逃逸原型 | PASS，Windows 实测 | `pnpm run verify:appcontainer` 输出 AppContainer Token=True、workspace read/write denied、loopback denied、child AppContainer=True、child protected-directory write denied=True。临时 profile 和工作目录在脚本结束时删除。 |
| AppContainer 接入 Runtime Shell/MCP/插件 | BLOCKED | 原型仅验证 Windows API 能力。Runtime Host 尚未使用 AppContainer launcher。为维持边界，Shell、MCP 和插件执行拒绝启动。 |

AppContainer 验证脚本在 `desktop/scripts/verify-appcontainer.ps1`。它调用 Windows `CreateAppContainerProfile`、`UpdateProcThreadAttribute(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES)` 和 `CreateProcessW`，并通过 token 检查验证实际进程身份。Probe 使用本地临时目录，不申请网络或文件能力，只将结果输出目录 ACL 授予 `ALL APPLICATION PACKAGES`。网络测试连接预先监听的 loopback 动态端口。子进程在容器中启动，并单独检查 AppContainer token 和写入结果。进程创建流程依据 [Microsoft's AppContainer launch guidance](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer) 和 [UpdateProcThreadAttribute documentation](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)。

该测试证明 Windows 可用原生 API 建立本地 AppContainer 隔离边界，不证明本产品的 Runtime 已经安全沙箱化，也没有验证用户工作区 capability 授权流程。当前拒绝 `run_command` 是有意安全限制。

## 运行验收

| 验收项 | 结果 | 证据/限制 |
| --- | --- | --- |
| Windows 开发模式 Electron 窗口启动 | PASS | 实际窗口显示中文状态页，Runtime 0.6.0、协议版本 1、Provider 未配置。 |
| Electron 启动现有 Python Host | PASS | 安装版进程树包含 `resources/agentdu-runtime/python/python.exe -B -X utf8 -m agentdu.desktop_host`。Host stderr 记录协议版本 1 启动。 |
| 安装目录含空格 | PASS | 最终构建的 NSIS 包安装至 `desktop/release/AgentDU D1 Final Install v2`，应用和 embedded Runtime 均启动。 |
| 无全局 Python | PASS | 安装版启动测试将 PATH 限制为 `%WINDIR%\System32`，找不到全局 `python.exe`。Runtime 进程来自安装目录的 embedded Python。 |
| 无 Provider 凭据 | PASS，状态测试 | Host 子进程环境采用变量 allowlist。界面显示未配置 Provider，测试覆盖缺少 Profile 与缺少凭据的错误路径。 |
| Provider 配置缺失 | PASS | Host 返回 `PROVIDER_NOT_CONFIGURED`，默认 UI 可运行不调用模型的受控测试。 |
| Runtime 启动失败 | PASS，单元测试 | RuntimeHostClient 的 spawn error 被转换为可见错误状态。 |
| Runtime 异常退出 | PASS，单元测试 | exit code 71 标记 `recoveredAfterCrash` 并拒绝重放活动任务。 |
| Runtime 输出损坏 IPC | PASS，单元测试 | 非 JSON stdout 导致 HostClient 进入 error 并终止 Runtime。 |
| Python 输入损坏 IPC | PASS，子进程测试 | 非法 JSON 和 `NaN` 输入发协议错误，退出码 2。 |
| Runtime 流式事件传输 | PASS，Python bundled 子进程测试 | 真实 AgentRuntime 任务通过 stdio JSONL 收到多条 `assistant_delta`。 |
| Python Runtime 侧实际取消 | PASS，Python bundled 子进程测试 | 取消请求返回 `cancelled`，同一 task 发出 `task.cancelled`，且未发 `task.completed`。 |
| 安装版 UI 触发并取消任务 | BLOCKED | Windows 输入代理报告目标点被其他应用窗口覆盖。激活目标并重新截图后唯一一次重试仍被覆盖，按界面自动化约束停止点击。Runtime 层取消已由子进程测试验证。 |
| 应用正常关窗与 Host orderly shutdown | BLOCKED | `host.shutdown` 和 stdin EOF 清洁退出有 Python 子进程测试。安装版窗口的正常关窗操作未能从 UI 实测。 |
| 安装版重启恢复提示 | PARTIAL | RuntimeClient 的 crash state 和启动标记代码已测试/审阅。实际安装版窗口异常关闭后再打开的提示未实测。 |

安装版恢复提示使用 `userData/desktop-launch.pending` 标记进程生命周期。应用启动时若已有标记则显示“Runtime 上次异常退出”，清洁关闭时先等待 Host shutdown 再移除标记。安装期间的崩溃会保留标记。Runtime task 不会自动重放。

## 自动化结果

最后一次完整 Python `unittest` 运行结果为 **79 项全部通过**，包含既有测试及全部 D1 Host 测试。相同 11 项 Host D1 测试也在 bundled Python 3.14.10 下单独运行并全部通过。Vitest **2 个文件、9 项通过**，覆盖 stdio schema、Runtime 进程启动错误、异常退出和坏 stdout 协议处理。

TypeScript `pnpm run build` 通过，包含 Renderer 与 Electron Main/Preload TypeScript 类型检查和 Vite production build。最终 NSIS x64 安装包为 `desktop/release/AgentDU-Setup-0.1.0-x64.exe`，大小 134,405,205 bytes，SHA-256 `6651FEF079344F39EB9B4A8D253DD6AC63B255F2CAACC0D11C75485D43D43495`。此安装包未做代码签名，electron-builder 使用默认 Electron 图标。

Windows AppContainer probe 命令为 `cd desktop; pnpm run verify:appcontainer`。Windows x64 NSIS 打包命令为 `cd desktop; $env:ELECTRON_MIRROR='https://npmmirror.com/mirrors/electron/'; pnpm run package:win`。安装包生成于 `desktop/release/AgentDU-Setup-0.1.0-x64.exe`。

## 本地运行

需要 Node.js 22.12+、pnpm 11、Python 3.12+。首次准备依赖后：

```powershell
cd desktop
pnpm install
pnpm run dev
```

生产构建与 Windows 安装包：

```powershell
cd desktop
pnpm run build
pnpm run prepare:runtime
pnpm run package:win
```

在 Electron 镜像网络不可达时设置 `ELECTRON_MIRROR` 为可访问镜像。Runtime ZIP 下载来自 python.org，并在打包脚本中强制 SHA-256 校验。NSIS 安装程序默认交互式运行，可更改安装目录。D1 版本没有凭据编辑 UI，Provider 配置继续读取 `%APPDATA%\AgentDU\config.toml` 和现有环境变量 Profile 引用。

## 新增文件

| 文件/目录 | 内容 |
| --- | --- |
| `desktop/` | Electron、Preload、React Renderer、IPC schema、构建配置、Vitest、Windows 打包和隔离探针 |
| `src/agentdu/desktop_host.py` | JSONL Runtime Host、请求方法、事件流、权限桥接和取消 |
| `tests/test_desktop_host.py` | Python Host 子进程协议、Provider、取消、退出测试 |
| `docs/desktop/d1-implementation.md` | D1 实现、边界和逐项验收记录 |
| `pyproject.toml` | 新增 `agentdu-desktop-host` 命令入口 |

桌面工程目录结构：

```text
desktop/
├── index.html
├── package.json
├── pnpm-lock.yaml
├── pnpm-workspace.yaml
├── README.md
├── scripts/
│   ├── prepare-runtime.ps1
│   └── verify-appcontainer.ps1
├── src/
│   ├── main/
│   │   ├── host-client.ts
│   │   └── index.ts
│   ├── preload/index.ts
│   ├── renderer/
│   │   ├── App.tsx
│   │   ├── global.d.ts
│   │   ├── main.tsx
│   │   └── styles.css
│   └── shared/
│       ├── protocol.ts
│       └── protocol.test.ts
├── tests/host-client.test.ts
├── tsconfig.electron.json
├── tsconfig.json
├── vite.config.ts
└── vitest.config.ts
```

未删除现有测试，未推送 GitHub。

## 需要解除的 D1 阻塞

1. 在无其他窗口拦截的桌面状态下，从最终安装版 UI 启动并取消受控任务。
2. 从安装版 UI 正常退出并确认 Host 以 0 退出、启动标记被移除。
3. 异常关闭后重启安装版并目视确认恢复提示。
4. 将 Python AppContainer launcher 接入需要执行的 Runtime 进程后，重新验证工作区 capability 授权，并覆盖文件、网络及子进程逃逸。集成前继续拒绝 Shell、MCP 与插件执行。

最终 Git 基线为 `701d3476d75671a6ea734f5fdd5c7dcf073e47c6`。D1 实现提交为 `109320dbfeda4fbb5ffbe1c84e22ef05a8e4dc24`，交付时的最终本地 HEAD 以验收记录提交为准。当前工作不推送 GitHub。

完成以上验证后才可将 D1 状态从 BLOCKED 更新为通过或逐项记录新的失败证据。
