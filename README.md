# BornFoRdeatH Codex Plugins

Public Codex plugin marketplace containing **Codex Usage Monitor 0.2.9**, with local telemetry and an
optional memory-only runtime UI for the Codex desktop app.

## Install from GitHub

```powershell
codex plugin marketplace add https://github.com/BornFoRdeatH/codex-usage-monitor.git
codex plugin add codex-usage-monitor@bornfordeath-plugins
```

Restart the Codex app, open a new task, and review the hook trust prompt. Then install the desktop
launcher with `scripts\usage-monitor.cmd ui install` from the installed plugin directory. Launch
Codex from **Codex Usage UI** to enable the persistent dock and verified message footers.

Plugin documentation: [plugins/codex-usage-monitor/README.md](plugins/codex-usage-monitor/README.md)
