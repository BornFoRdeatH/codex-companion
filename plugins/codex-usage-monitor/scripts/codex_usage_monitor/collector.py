from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import LoadedConfig, load_config
from .storage import Storage


def ensure_collector(plugin_root: Path, plugin_data: Path, storage: Storage) -> bool:
    if os.environ.get("CODEX_USAGE_MONITOR_NO_COLLECTOR") == "1":
        return False
    storage.set_meta("collector_last_ping", str(time.time()))
    try:
        retry_after = float(storage.get_meta("collector_retry_after", "0"))
    except ValueError:
        retry_after = 0.0
    if retry_after > time.time():
        return False
    pid_file = plugin_data / "collector.pid"
    if _pid_alive(pid_file):
        return True
    start_lock = plugin_data / "collector.starting"
    try:
        descriptor = os.open(start_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(descriptor)
    except FileExistsError:
        try:
            if time.time() - start_lock.stat().st_mtime < 10:
                return True
            start_lock.unlink()
        except OSError:
            return True
    command = [sys.executable, str(plugin_root / "scripts" / "collector.py"), "--serve"]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(plugin_root),
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
        return True
    except OSError:
        return False
    finally:
        try:
            start_lock.unlink()
        except OSError:
            pass


def serve(plugin_root: Path, plugin_data: Path) -> int:
    config = load_config(plugin_root, plugin_data)
    storage = Storage(Path(config.get("storage.database")))
    pid_file = plugin_data / "collector.pid"
    lifetime_lock = plugin_data / "collector.lock"
    if _pid_alive(pid_file):
        storage.close()
        return 0
    try:
        lock_fd = os.open(lifetime_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
    except FileExistsError:
        if _pid_alive(pid_file):
            storage.close()
            return 0
        try:
            lifetime_lock.unlink()
            lock_fd = os.open(lifetime_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(lock_fd)
        except (FileExistsError, OSError):
            storage.close()
            return 0
    try:
        pid_file.write_text(str(os.getpid()), encoding="ascii")
        storage.set_meta("collector_pid", str(os.getpid()))
        storage.set_meta("collector_error", "")
        return _run_app_server(config, storage)
    finally:
        try:
            if pid_file.exists() and pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass
        try:
            lifetime_lock.unlink()
        except OSError:
            pass
        storage.set_meta("collector_pid", "")
        storage.close()


def find_codex_executable() -> str | None:
    candidates = [
        shutil.which("codex"),
        str(Path.home() / ".codex" / "plugins" / ".plugin-appserver" / ("codex.exe" if os.name == "nt" else "codex")),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _run_app_server(config: LoadedConfig, storage: Storage) -> int:
    codex = find_codex_executable()
    if not codex:
        storage.set_meta("collector_error", "codex executable not found")
        storage.set_meta("collector_retry_after", str(time.time() + 300))
        return 1
    try:
        proc = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        storage.set_meta("collector_error", f"cannot start app-server: {exc}")
        storage.set_meta("collector_retry_after", str(time.time() + 300))
        return 1
    storage.set_meta("collector_retry_after", "0")
    messages: queue.Queue[dict[str, Any]] = queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    threading.Thread(target=reader, daemon=True).start()
    assert proc.stdin is not None

    def send(message: dict[str, Any]) -> None:
        proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    send(
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "codex_usage_monitor",
                    "title": "Codex Usage Monitor",
                    "version": "0.2.7",
                }
            },
        }
    )
    send({"method": "initialized", "params": {}})
    request_id = 10
    last_rate = 0.0
    last_usage = 0.0
    rate_interval = float(config.get("refresh.rate_limits_min_interval_seconds", 5))
    usage_interval = float(config.get("refresh.account_usage_min_interval_seconds", 300))
    while proc.poll() is None:
        now = time.time()
        try:
            last_ping = float(storage.get_meta("collector_last_ping", str(now)))
        except ValueError:
            last_ping = now
        if now - last_ping > 900:
            break
        if config.get("data_sources.rate_limits", True) and now - last_rate >= rate_interval:
            request_id += 1
            send({"method": "account/rateLimits/read", "id": request_id})
            storage.set_meta(f"request:{request_id}", "rates")
            last_rate = now
        if config.get("data_sources.account_usage", True) and now - last_usage >= usage_interval:
            request_id += 1
            send({"method": "account/usage/read", "id": request_id})
            storage.set_meta(f"request:{request_id}", "usage")
            last_usage = now
        try:
            message = messages.get(timeout=0.25)
        except queue.Empty:
            continue
        _handle_message(storage, message)
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    return 0


def _handle_message(storage: Storage, message: dict[str, Any]) -> None:
    now = time.time()
    method = message.get("method")
    if method == "account/rateLimits/updated":
        params = message.get("params")
        if isinstance(params, dict):
            storage.add_rate_limits(params, now, "official_app_server")
        return
    message_id = message.get("id")
    if message_id is None or "result" not in message:
        return
    kind = storage.get_meta(f"request:{message_id}")
    result = message.get("result")
    if not isinstance(result, dict):
        return
    if kind == "rates":
        storage.add_rate_limits(result, now, "official_app_server")
    elif kind == "usage":
        storage.add_account_usage(result, now, "official_app_server")


def _pid_alive(pid_file: Path) -> bool:
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid():
        return False
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return str(pid) in result.stdout
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
