# Codex Usage Monitor 0.2.1

Local Codex token, context, quota, operation, subagent, and account telemetry. Version 0.2 adds an
optional runtime UI: a persistent resizable dock plus compact telemetry footers below commentary
and final answers on explicitly supported Codex desktop builds.

The UI is injected in memory over a random loopback Chromium DevTools port. It does not modify
Codex files, `app.asar`, package signatures, or model context. Hooks collect telemetry but are not
used as the display surface while `[ui]` is enabled.

## Install from GitHub

```powershell
codex plugin marketplace add https://github.com/BornFoRdeatH/codex-usage-monitor.git
codex plugin add codex-usage-monitor@bornfordeath-plugins
```

Restart Codex, create a new task, and approve the hook trust prompt after reviewing
`hooks/hooks.json`.

## Install the desktop UI launcher

Run the command from the installed plugin directory. On Windows it creates Desktop and Start Menu
shortcuts; on macOS it creates `~/Applications/Codex Usage UI.app`; on Linux it creates a desktop
entry.

```powershell
scripts\usage-monitor.cmd ui install
scripts\usage-monitor.cmd ui doctor
```

Fully close a normally launched Codex instance, then start **Codex Usage UI**. Codex must be started
by this launcher because v0.2 deliberately does not attach to arbitrary existing processes.

After updating the plugin, run `ui install` again so the shortcut points to the current installed
plugin version. Remove it with `ui uninstall`.

## Compatibility behavior

Footer mounting is enabled only when both the installed package version and `app.asar` SHA-256
match an entry in `ui/adapters.json`. The included adapter supports Windows Codex
`26.707.12708.0`. An unknown or updated build gets a compatibility notice and a persistent dock,
but no heuristic message selectors or footers. Run `ui adapters` to inspect the live fingerprint.

The dock can be resized, collapsed, and moved among right, bottom, left, and floating placements.
By default `layout_mode = "reserve_space"` shrinks the Codex `#root` viewport so docked panels do
not cover navigation, the composer, or message content. Set `layout_mode = "overlay"` for the old
overlay behavior; `floating` is always an overlay.
Its layout is stored in renderer-local state. Completed-message snapshots are stored in the
plugin SQLite database by `thread_id + item_id`; message text is never read or stored.

## Widgets

Widget directories are configured under `[ui.widgets]`:

- built in: `${PLUGIN_ROOT}/ui/widgets`;
- personal: `${PLUGIN_DATA}/ui/widgets`.

Each widget lives in its own directory and contains `manifest.json` schema v1:

```json
{
  "schema_version": 1,
  "id": "my-widget",
  "name": "My Widget",
  "entry": "widget.html",
  "content_type": "html",
  "placements": ["right_dock", "floating"],
  "default_placement": "right_dock",
  "permissions": ["telemetry", "theme", "resize"],
  "order": 100,
  "size": {"width": 320, "height": 180}
}
```

Supported content types are `markdown`, sanitized `html`/CSS, and sandboxed `javascript`.
Scripted widgets run in iframes without `allow-same-origin`; CSP denies network, navigation,
popups, forms, downloads, and filesystem access. Their capability API is limited to
`getSnapshot`, `subscribeTelemetry`, `getTheme`, `requestResize`, and `openSettings`.
`message_footer` widgets must be declarative and share the single footer renderer.

## Requirements and data sources

- Python 3.11+ (`python3` on macOS/Linux, `py -3` on Windows).
- Codex desktop for the runtime UI; hooks and the CLI still work without it.
- Optional `codex app-server` for official account/rate-limit refresh. On Windows the collector
  also discovers `~/.codex/plugins/.plugin-appserver/codex.exe`.

| Data | Source | Provenance |
| --- | --- | --- |
| Account usage | `account/usage/read` | official |
| Rate limits | App Server read/update | official |
| Thread/model-call tokens | rollout transcript `token_count` | experimental |
| Tool lifecycle | Codex hooks/transcript | observed |
| Context remaining and forecasts | local calculations | estimated (`≈`) |

The detached collector is single-instance. Warm hooks only ping it, incrementally parse the
transcript, and read/write SQLite snapshots. Failed App Server starts use a five-minute backoff.

## Configuration

On first use, `config.default.toml` is copied to `%PLUGIN_DATA%/config.toml`. When the CLI is run
outside a hook, it resolves the active marketplace data directory under `~/.codex/plugins/data`.
Version 0.2 keeps
`schema_version = 1` and adds `[ui]`, `[ui.widgets]`, and `[ui.security]`. Existing configs inherit
new defaults. Unknown keys warn and invalid values fall back safely.

The privacy invariants `never_store_auth_tokens`, `never_store_prompt_contents`,
`page_dom_denied`, `message_contents_denied`, and `network_denied` cannot be disabled. Raw prompts,
assistant text, auth tokens, tool inputs/outputs, and raw events are not stored by default.

## CLI

```powershell
scripts\usage-monitor.cmd status
scripts\usage-monitor.cmd doctor
scripts\usage-monitor.cmd config-path
scripts\usage-monitor.cmd validate-config
scripts\usage-monitor.cmd export-summary
scripts\usage-monitor.cmd reset-cache --yes
scripts\usage-monitor.cmd ui install
scripts\usage-monitor.cmd ui launch
scripts\usage-monitor.cmd ui uninstall
scripts\usage-monitor.cmd ui doctor
scripts\usage-monitor.cmd ui status
scripts\usage-monitor.cmd ui adapters
```

On macOS/Linux use `scripts/usage-monitor` with the same arguments. Set
`CODEX_USAGE_MONITOR_DATA` or pass `--data-dir` to inspect a non-default data directory.

## Trust and limitations

Review and trust the lifecycle hooks before enabling them. CDP is bound to `127.0.0.1`, and the
host rejects non-loopback WebSocket endpoints. Anyone able to execute code as the same OS user may
still inspect that user's processes; this launcher is not a security boundary against a compromised
account.

`PostToolUse` does not cover every internal operation. WebSearch, Computer Use, and some shell paths
are counted only when a compatible transcript event exists. Subscription capacity and monetary cost
are not inferred from percentage-only account data.
