from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
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


def launch_codex(executable: Path, port: int) -> subprocess.Popen[bytes]:
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


def launcher_paths() -> list[Path]:
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
    script = plugin_root / "scripts" / ("usage-monitor.cmd" if system == "Windows" else "usage-monitor")
    executable = discover_codex_app(plugin_data)
    if executable:
        plugin_data.mkdir(parents=True, exist_ok=True)
        (plugin_data / "ui-app-path.txt").write_text(str(executable), encoding="utf-8")
    if system == "Windows":
        wrapper = plugin_data / "ui" / "codex-usage-ui.cmd"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(
            f'@echo off\r\npy -3 "{plugin_root / "scripts" / "usage_monitor.py"}" --data-dir "{plugin_data}" ui launch\r\n',
            encoding="utf-8",
        )
        command_processor = Path(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"))
        ps = (
            "$w=New-Object -ComObject WScript.Shell;"
            + ";".join(
                f"$s=$w.CreateShortcut('{_ps(path)}');$s.TargetPath='{_ps(command_processor)}';"
                f"$s.Arguments='/d /c \"\"{_ps(wrapper)}\"\"';$s.WorkingDirectory='{_ps(plugin_root)}';"
                "$s.WindowStyle=7;$s.Description='Codex Usage Monitor runtime UI';$s.Save()"
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
        executable = macos / "codex-usage-ui"
        executable.write_text(f'#!/bin/sh\nexec "{script}" --data-dir "{plugin_data}" ui launch\n', encoding="utf-8")
        executable.chmod(0o755)
        (app / "Contents" / "Info.plist").write_text(
            """<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>codex-usage-ui</string><key>CFBundleIdentifier</key><string>local.codex.usage-ui</string>
<key>CFBundleName</key><string>Codex Usage UI</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>""",
            encoding="utf-8",
        )
    else:
        desktop = paths[0]
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(
            "[Desktop Entry]\nType=Application\nName=Codex Usage UI\n"
            f'Exec="{script}" --data-dir "{plugin_data}" ui launch\nTerminal=false\nCategories=Development;Utility;\n',
            encoding="utf-8",
        )
        desktop.chmod(0o755)
    return paths


def uninstall_launcher(plugin_data: Path | None = None) -> list[Path]:
    removed: list[Path] = []
    for path in launcher_paths():
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
        elif path.exists():
            path.unlink()
            removed.append(path)
    if plugin_data:
        wrapper = plugin_data / "ui" / "codex-usage-ui.cmd"
        wrapper.unlink(missing_ok=True)
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
    return value


def _ps(path: Path) -> str:
    return str(path).replace("'", "''")


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
    result: list[Path] = []
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
                        if "OpenAI.Codex_" in str(path) and path not in result:
                            result.append(path)
                    kernel.CloseHandle(handle)
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return result
