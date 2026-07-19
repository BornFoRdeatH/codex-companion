from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def discover_codex_app(plugin_data: Path | None = None) -> Path | None:
    system = platform.system()
    candidates: list[Path] = []
    override = os.environ.get("CODEX_DESKTOP_EXECUTABLE")
    if override:
        candidates.append(Path(override))
    if plugin_data:
        try:
            candidates.append(Path((plugin_data / "ui-app-path.txt").read_text(encoding="utf-8").strip()))
        except OSError:
            pass
    if system == "Windows":
        candidates.extend(_windows_running_paths())
        roots = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"]
        for root in roots:
            try:
                packages = sorted(root.glob("OpenAI.Codex_*_x64__2p2nqsd0c76g0"), reverse=True)
            except OSError:
                packages = []
            candidates.extend(package / "app" / "ChatGPT.exe" for package in packages)
        candidates.extend([Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Codex" / "Codex.exe"])
    elif system == "Darwin":
        candidates.extend(
            [
                Path("/Applications/Codex.app/Contents/MacOS/Codex"),
                Path.home() / "Applications" / "Codex.app" / "Contents" / "MacOS" / "Codex",
                Path("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"),
            ]
        )
    else:
        for name in ("codex-desktop", "codex", "chatgpt"):
            value = shutil.which(name)
            if value:
                candidates.append(Path(value))
        candidates.extend([Path("/opt/Codex/codex"), Path("/opt/codex/codex")])
    result = next((path for path in candidates if path.is_file()), None)
    if result and plugin_data:
        try:
            plugin_data.mkdir(parents=True, exist_ok=True)
            (plugin_data / "ui-app-path.txt").write_text(str(result), encoding="utf-8")
        except OSError:
            pass
    return result


def launch_codex(executable: Path, port: int, restart_existing: bool = False) -> subprocess.Popen[bytes]:
    if restart_existing:
        restart_existing_codex(executable)
    command = [
        str(executable),
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
    ]
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def restart_existing_codex(executable: Path, timeout: float = 8.0) -> int:
    """Stop the existing Windows single-instance tree before a launcher restart."""
    if os.name != "nt":
        return 0
    target = str(executable).lower()
    entries = [entry for entry in _windows_process_entries() if str(entry[2]).lower() == target]
    if not entries:
        return 0
    target_pids = {entry[0] for entry in entries}
    roots = [entry[0] for entry in entries if entry[1] not in target_pids]
    for pid in roots or sorted(target_pids):
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(str(entry[2]).lower() == target for entry in _windows_process_entries()):
            return len(entries)
        time.sleep(0.1)
    raise RuntimeError("The existing Codex process did not exit; close it in Task Manager and retry")


def launcher_paths() -> list[Path]:
    system = platform.system()
    if system == "Windows":
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        start = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        return [desktop / "Codex Companion.lnk", start / "Codex Companion.lnk"]
    if system == "Darwin":
        return [Path.home() / "Applications" / "Codex Companion.app"]
    return [Path.home() / ".local" / "share" / "applications" / "codex-companion.desktop"]


def legacy_launcher_paths() -> list[Path]:
    system = platform.system()
    if system == "Windows":
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        start = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        return [desktop / "Codex Usage UI.lnk", start / "Codex Usage UI.lnk"]
    if system == "Darwin":
        return [Path.home() / "Applications" / "Codex Usage UI.app"]
    return [Path.home() / ".local" / "share" / "applications" / "codex-usage-ui.desktop"]


def install_launcher(plugin_root: Path, plugin_data: Path) -> list[Path]:
    system = platform.system()
    paths = launcher_paths()
    plugin_data = _user_visible_path(plugin_data)
    plugin_root = _user_visible_path(plugin_root, plugin_data)
    launcher_dir = plugin_data / "ui"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    for legacy in legacy_launcher_paths():
        if legacy.is_dir():
            shutil.rmtree(legacy)
        else:
            legacy.unlink(missing_ok=True)
    (launcher_dir / "codex-usage-ui.cmd").unlink(missing_ok=True)
    bootstrap = launcher_dir / "launcher.py"
    bootstrap.write_text(_bootstrap_source(_plugin_family(plugin_root), plugin_data), encoding="utf-8")
    executable = discover_codex_app(plugin_data)
    if executable:
        plugin_data.mkdir(parents=True, exist_ok=True)
        (plugin_data / "ui-app-path.txt").write_text(str(executable), encoding="utf-8")
    if system == "Windows":
        wrapper = launcher_dir / "codex-companion.cmd"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(
            f'@echo off\r\npy -3 "{bootstrap}" --restart-existing %*\r\n',
            encoding="utf-8",
        )
        python_gui = Path(shutil.which("pythonw") or shutil.which("pyw") or sys.executable)
        python_gui_args = "-3 " if python_gui.name.lower() == "pyw.exe" else ""
        ps = (
            "$w=New-Object -ComObject WScript.Shell;"
            + ";".join(
                f"$s=$w.CreateShortcut('{_ps(path)}');$s.TargetPath='{_ps(python_gui)}';"
                f"$s.Arguments='{python_gui_args}\"{_ps(bootstrap)}\" --restart-existing';$s.WorkingDirectory='{_ps(launcher_dir)}';"
                "$s.WindowStyle=7;$s.Description='Codex Companion runtime UI';$s.Save()"
                for path in paths
            )
        )
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], check=True)
    elif system == "Darwin":
        app = paths[0]
        macos = app / "Contents" / "MacOS"
        macos.mkdir(parents=True, exist_ok=True)
        executable = macos / "codex-companion"
        executable.write_text(f'#!/bin/sh\nexec python3 "{bootstrap}"\n', encoding="utf-8")
        executable.chmod(0o755)
        (app / "Contents" / "Info.plist").write_text(
            """<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>codex-companion</string><key>CFBundleIdentifier</key><string>local.codex.companion</string>
<key>CFBundleName</key><string>Codex Companion</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>""",
            encoding="utf-8",
        )
    else:
        desktop = paths[0]
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(
            "[Desktop Entry]\nType=Application\nName=Codex Companion\n"
            f'Exec=python3 "{bootstrap}"\nTerminal=false\nCategories=Development;Utility;\n',
            encoding="utf-8",
        )
        desktop.chmod(0o755)
    return paths


def uninstall_launcher(plugin_data: Path | None = None) -> list[Path]:
    removed: list[Path] = []
    for path in launcher_paths() + legacy_launcher_paths():
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
        elif path.exists():
            path.unlink()
            removed.append(path)
    if plugin_data:
        for name in ("codex-companion.cmd", "codex-usage-ui.cmd"):
            (plugin_data / "ui" / name).unlink(missing_ok=True)
    return removed


def status(plugin_data: Path) -> dict[str, Any]:
    status_path = plugin_data / "ui-status.json"
    value: dict[str, Any] = {}
    if status_path.is_file():
        try:
            value = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {"error": "invalid status file"}
    value["launchers"] = [{"path": str(path), "installed": path.exists()} for path in launcher_paths()]
    value["codex_executable"] = str(discover_codex_app(plugin_data) or "")
    launcher_error = plugin_data / "ui" / "launcher-error.log"
    if launcher_error.is_file():
        try:
            value["launcher_error"] = launcher_error.read_text(encoding="utf-8")
        except OSError:
            value["launcher_error"] = "unreadable launcher error log"
    return value


def _ps(path: Path) -> str:
    return str(path).replace("'", "''")


def _plugin_family(plugin_root: Path) -> Path:
    """Return a stable root that survives versioned plugin cache replacement."""
    if "cache" in (part.lower() for part in plugin_root.parts) and plugin_root.parent.name == "codex-usage-monitor":
        return plugin_root.parent
    return plugin_root


def _bootstrap_source(plugin_family: Path, plugin_data: Path) -> str:
    return f'''from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

family = Path({str(plugin_family)!r})
error_log = Path({str(plugin_data / "ui" / "launcher-error.log")!r})
direct = family / "scripts" / "usage_monitor.py"
candidates = [family] if direct.is_file() else [
    path for path in family.iterdir()
    if path.is_dir() and (path / "scripts" / "usage_monitor.py").is_file()
] if family.is_dir() else []
if not candidates:
    message = "Codex Companion is not installed. Reinstall the codex-usage-monitor plugin and run ui install."
    error_log.write_text(message, encoding="utf-8")
    print(message, file=sys.stderr)
    raise SystemExit(2)
root = max(candidates, key=lambda path: ((path / "scripts" / "usage_monitor.py").stat().st_mtime, path.name))
script = root / "scripts" / "usage_monitor.py"
if "--check" in sys.argv:
    print(script)
    raise SystemExit(0)
forward = [value for value in sys.argv[1:] if value != "--check"]
error_log.unlink(missing_ok=True)
try:
    completed = subprocess.run(
        [sys.executable, str(script), "--data-dir", {str(plugin_data)!r}, "ui", "launch", *forward],
        check=False,
    )
    raise SystemExit(completed.returncode)
except OSError as exc:
    message = f"Cannot launch Codex Companion: {{exc}}"
    error_log.write_text(message, encoding="utf-8")
    print(message, file=sys.stderr)
    raise SystemExit(2)
'''


def _user_visible_path(path: Path, reference_data: Path | None = None) -> Path:
    parts = path.parts
    if os.name == "nt" and len(parts) > 3 and parts[1].lower() == "users" and parts[2].lower().startswith("codexsandbox"):
        real_home = None
        if reference_data and ".codex" in reference_data.parts:
            real_home = Path(*reference_data.parts[: reference_data.parts.index(".codex")])
        real_home = real_home or Path(os.environ.get("USERPROFILE") or Path.home())
        return real_home.joinpath(*parts[3:])
    return path


def _windows_running_paths() -> list[Path]:
    result: list[Path] = []
    for _, _, path in _windows_process_entries():
        if path not in result:
            result.append(path)
    return result


def _windows_process_entries() -> list[tuple[int, int, Path]]:
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    result: list[tuple[int, int, Path]] = []
    try:
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in {"chatgpt.exe", "codex.exe"}:
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if handle:
                    buffer = ctypes.create_unicode_buffer(32768)
                    length = wintypes.DWORD(len(buffer))
                    if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                        path = Path(buffer.value)
                        if "OpenAI.Codex_" in str(path):
                            result.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), path))
                    kernel.CloseHandle(handle)
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return result
