# BornFoRdeatH Codex Plugins

Public Codex plugin marketplace containing **Codex Companion 1.1.0** (technical plugin ID `codex-usage-monitor`), with Safe Handoff Builder, a responsive Control Center, advisory Budget Planner, Project Insights, Smart Performance Mode, session-isolated local telemetry, Native History Focus, Usage Advisor, Usage Guard, and
optional memory-only runtime UI for the Codex desktop app.

## Install from GitHub

```powershell
codex plugin marketplace add https://github.com/BornFoRdeatH/codex-companion.git
codex plugin add codex-usage-monitor@bornfordeath-plugins
```

Restart the Codex app, open a new task, and review the hook trust prompt. Then install the desktop
launcher with `scripts\usage-monitor.cmd ui install` from the installed plugin directory. Launch
Codex from **Codex Companion** to enable the persistent dock, Native History Focus, and verified message footers.

Plugin documentation: [plugins/codex-companion/README.md](plugins/codex-companion/README.md)
