# Codex Usage Monitor

Personal Codex plugin that shows local token, context, quota, operation, subagent, and account
telemetry through lifecycle-hook `systemMessage` output.

The monitor never returns `additionalContext`, and its default privacy policy does not persist
prompts, assistant text, authentication tokens, tool inputs, or tool outputs.

## Install from GitHub

```powershell
codex plugin marketplace add https://github.com/BornFoRdeatH/codex-usage-monitor.git
codex plugin add codex-usage-monitor@bornfordeath-plugins
```

Restart Codex, create a new task, and approve the hook trust prompt after reviewing
`hooks/hooks.json`.

## Requirements

- Codex with plugin lifecycle hooks.
- Python 3.11 or newer (`python3` on macOS/Linux, `py -3` on Windows).
- Optional callable `codex app-server` for official account usage and rate-limit refreshes.
  When unavailable, thread token and rate-limit snapshots fall back to the current transcript JSONL.

## Data sources

| Data | Source | Quality |
| --- | --- | --- |
| Account usage | `account/usage/read` | official |
| Rate limits | App Server read/update | official |
| Thread/model-call tokens | rollout transcript `token_count` | experimental |
| Tool lifecycle | Codex hooks | observed |
| Context remaining and forecasts | local calculations | estimated |

The collector is a detached, single-instance helper. Hooks only ping it and read SQLite snapshots,
so warm hook execution remains local. It exits after 15 minutes without hook activity. Failed
App Server launches use a five-minute retry backoff.

## Configuration

On first use, `config.default.toml` is copied to:

```text
%PLUGIN_DATA%/config.toml
```

Every display event and field group can be enabled independently. Invalid values fall back to
defaults and are written to the diagnostics log when diagnostics are enabled. The privacy
invariants `never_store_auth_tokens` and `never_store_prompt_contents` cannot be disabled.

## CLI

Windows:

```powershell
scripts\usage-monitor.cmd status
scripts\usage-monitor.cmd doctor
scripts\usage-monitor.cmd config-path
scripts\usage-monitor.cmd validate-config
scripts\usage-monitor.cmd export-summary
scripts\usage-monitor.cmd reset-cache --yes
```

macOS/Linux:

```sh
scripts/usage-monitor status
```

Set `CODEX_USAGE_MONITOR_DATA` to inspect a non-default data directory.

## Trust and limitations

Codex requires review and trust before non-managed plugin hooks run. `PostToolUse` does not cover
every internal operation; WebSearch, Computer Use, and some shell paths are reported only if a
compatible transcript record exists. Subscription quota capacity and monetary cost are not
invented from percentage-only account data.
