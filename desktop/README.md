# AgentDU Desktop

AgentDU Desktop D1 is a local Electron application backed by the existing Python AgentDU Runtime.

## Development

Requirements are Node.js 22.12 or newer, pnpm 11, and Python 3.12 or newer.

```powershell
pnpm install
pnpm run dev
```

The development Runtime starts from the repository's active Python environment. Override it with `AGENTDU_DESKTOP_PYTHON` when needed.

## Windows package

```powershell
pnpm run build
pnpm run prepare:runtime
pnpm run package:win
```

`prepare:runtime` downloads the Python.org Python 3.14.10 Windows x64 embeddable package and validates its SHA-256 before adding the AgentDU Runtime source. `package:win` creates an x64 NSIS installer in `release/`.

## Checks

```powershell
pnpm test
pnpm run verify:appcontainer
```

The AppContainer command tests an isolation prototype. It does not activate Shell, MCP, or plugin execution in the desktop Runtime.

See [`docs/desktop/d1-implementation.md`](../docs/desktop/d1-implementation.md) for the D1 IPC contract, implementation record, test evidence, and known validation blockers.
