# Codex Usage Monitor 0.5.0

Local Codex token, context, quota, operation, subagent, and account telemetry. Version 0.2 adds an
optional runtime UI: a persistent resizable dock plus compact telemetry footers below commentary
and final answers on explicitly supported Codex desktop builds.

The UI is injected in memory over a random loopback Chromium DevTools port. It does not modify
Codex files, `app.asar`, package signatures, or model context. Hooks collect telemetry but are not
used as the display surface while `[ui]` is enabled.

## Chat history virtualization

Long chats use privacy-safe soft virtualization by default. The runtime keeps the latest 10 complete
turns in layout and paint, while older complete turn wrappers remain mounted in React but use
`display: none` and containment markers. A native-style **Show previous 10** button reveals history
in batches without changing the current scroll position. Streaming turns are never hidden.

Turn wrappers are identified only from structural `conversationId`, `turnId`, and completion state;
prompt, assistant, and tool contents are not read. The window resets after reload or task switch.
Unknown renderer builds must first present at least three unique turns under the same structural
parent; if this contract is not confirmed, every wrapper is restored and virtualization stays off.

```toml
[ui.chat_virtualization]
enabled = true
visible_turns = 10
load_batch = 10
reset_on_thread_switch = true
unknown_version_policy = "probe"
```

`visible_turns` and `load_batch` accept values from 5 through 100. Setting `enabled = false`
immediately restores Codex's standard layout. Diagnostics contain only thread ID, compatibility,
and visible/hidden turn counts in `ui-status.json`.

## Session isolation

The desktop runtime reads only the active composer's structural `conversationId`. Thread tokens,
context, tools, compactions, and turn metrics are selected inside that session; account quota remains
global. A new or not-yet-registered chat shows unavailable values instead of borrowing another chat's
latest turn. `ui-status.json` records the active thread, its state, and the last switch timestamp.

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

After updating the plugin, run `ui install` again to refresh the stable launcher bootstrap.
Remove it with `ui uninstall`.

Since 0.2.5 the installed shortcut calls a stable bootstrap under `%PLUGIN_DATA%/ui`. The bootstrap
selects the newest intact marketplace cache entry at launch time, so removing an obsolete version
directory no longer breaks the Desktop or Start Menu shortcut.

On Windows the installed shortcut passes `--restart-existing`: it closes an existing background
Codex process tree before starting the app with its loopback DevTools port. This is required because
Codex is single-instance and may remain running after its last visible window is closed. Direct
`ui launch` calls remain non-destructive unless the flag is supplied explicitly.

The stable bootstrap invokes the selected plugin through `subprocess` with an argument vector.
This preserves Windows interpreter paths containing spaces, including
`C:\Program Files\Python312`, instead of relying on `os.execv` command-line parsing.

## Compatibility behavior

An exact package version plus `app.asar` SHA-256 match in `ui/adapters.json` enables footers
immediately. The included adapters support Windows Codex `26.707.12708.0`, `26.715.3651.0`, and
`26.715.4045.0`.
For an unknown build, the dock starts in a non-blocking probing state. It enables footers only
after finding a known item contract once or the same privacy-safe structural contract on multiple
items; absence of an allowlisted fingerprint alone is no longer treated as incompatibility.
The probe reads only item/thread/turn identifiers, phase and completion state. Run `ui adapters`
to inspect the exact fingerprint registry.

The dock can be resized, collapsed, and moved among right, bottom, left, and floating placements.
By default `layout_mode = "reserve_space"` shrinks both the Codex `#root` viewport and its
viewport-sized application shell so docked panels do not cover the native right sidebar,
navigation, composer, or message content. Set `layout_mode = "overlay"` for the old
overlay behavior; `floating` is always an overlay.
On Electron builds the dock detects the top `-webkit-app-region: drag` titlebar and starts below
it, leaving native minimize, maximize and close controls unobstructed.
Its layout is stored in renderer-local state. Completed-message snapshots are stored in the
plugin SQLite database by `thread_id + item_id`; message text is never read or stored.

With `composer_toggle = true` (the default), a native-style side-panel icon is inserted after the
left composer controls. It hides or restores only the usage dock and persists the choice in
renderer `localStorage` as `codexUsageDockVisible`; inline footers remain visible. If the structural
composer anchor cannot be found, the dock is forced visible so the control can never lock itself
out. Set `composer_toggle = false` to disable this button and ignore hidden state.

The native-style footer is correlated to its real Codex `conversationId + turnId`, not to the
latest global snapshot. It shows that request's total/input/output/reasoning tokens, native context
remaining, rate-limit remaining, estimated quota delta (`≈`), and observed execution time. Multiple
commentary items belonging to one request intentionally share turn-level usage because Codex does
not expose an authoritative per-fragment token allocation. Each footer is
an inline Shadow DOM child of its message container, so scrolling and virtualization move it as
part of the native layout rather than through a delayed fixed-position overlay. Rate-window
labels come from `window_minutes`; a seven-day snapshot is never labeled as a five-hour window.
The dock exposes the token breakdown, context window, cache hit, reset countdown, tools,
compactions, subagents, account fields when available, and data provenance.

Context usage prefers the privacy-safe native renderer percentage used by Codex's own composer
indicator. `last_input_tokens` is retained only as an estimated fallback because it can include
cached/replayed input and does not necessarily equal the occupied context after compaction.
Historical messages without a captured native percentage show context as unavailable instead of
reusing the current request's value.

The built-in **Usage Summary** widget is live: it shows current request tokens, context remaining,
the longest available rate-window remainder, and its update time. It is no longer a static
"Live telemetry" placeholder.

### Usage Guard and History

Usage Guard activates the existing used-percent thresholds for account quota, current context,
slow/expensive turns, and low cache hit. Its composer badge and dock banner are locally dismissible
with a 15-minute per-condition cooldown. Estimated context is marked `≈` and cannot create a critical
alert; critical context requires official or renderer-observed data.

The chart button opens localized **Usage History**. It defaults to the current chat over seven days,
with current/all-chat scopes and 24h/7d/30d/all ranges. History is loaded only when requested over the
CDP binding and rendered as dependency-free SVG. Labels contain only timestamp, model, and a short
session ID; chat titles and message contents are never read.

The UI host sends live snapshots at `ui.refresh_interval_ms`, writes a five-second heartbeat to
`ui-status.json`, and reconnects after transient CDP or SQLite errors. Diagnostics are retained in
`ui-host-error.log`; prompt, response, and tool contents are never written there.

### Usage Advisor

The local **Usage Advisor** recommends one concrete token-saving action from measured telemetry.
It can suggest starting a new chat when renderer-observed context is at least 85%, avoiding new
scope after repeated compactions, narrowing an unusually expensive request, reducing exploration
after excessive or failed tool calls, using lower reasoning effort for a tool-light outlier, or
conserving a nearly exhausted global quota. Advice describes the observed signal; it does not judge
answer quality and never calls a model.

After ten completed turns, the Advisor compares the current turn with the median and median absolute
deviation of the last 50 completed turns for the same model. Until then, conservative fixed
thresholds work immediately. Unfinished turns, unavailable values, reset discontinuities, and turns
from other models are excluded. The dock shows one prioritized tip, warning/critical tips add a
composer badge, and completed final-answer footers retain their turn's tip. Clicking the dock tip
shows numeric evidence, confidence, provenance, and the suggested action; dismiss state has a
30-minute per-type cooldown. History displays advice markers and the personal token baseline.

Prompt Coach is a separate opt-in feature. When enabled, the `UserPromptSubmit.prompt` hook field is
examined locally for structural signals in Ukrainian and English, then immediately discarded. Only
numeric counts and recommendation codes may be retained; prompt text, fragments, matched phrases,
and hashes never enter SQLite, logs, snapshots, CDP payloads, or exports.

```toml
[ui.advisor.prompt_coach]
enabled = false
store_derived_features = true
```

### Localization

`ui.auto_locale = true` follows the Codex/OS browser locale. Ukrainian (`uk`) and English (`en`)
are the only UI dictionaries. Every other locale, including Russian, falls back to English.
Set `auto_locale = false` and `locale.language = "uk"` or `"en"` for an explicit override.

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
Version 0.4 keeps public config `schema_version = 1`, uses internal SQLite schema v3, and adds
`[ui.advisor]` plus opt-in `[ui.advisor.prompt_coach]`. Existing configs inherit
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
scripts\usage-monitor.cmd export-history --session-id <id> --since 7d --format json
scripts\usage-monitor.cmd advice --session-id <id>
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
