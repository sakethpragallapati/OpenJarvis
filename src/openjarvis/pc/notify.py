"""Desktop notifications (Windows toast, macOS Notification Center, notify-send)."""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

# Title and message reach PowerShell through environment variables and are
# inserted with CreateTextNode, so no user text is ever parsed as script/XML.
_WINDOWS_TOAST = r"""
$ErrorActionPreference = 'Stop'
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $nodes = $xml.GetElementsByTagName('text')
    $nodes.Item(0).AppendChild($xml.CreateTextNode($env:OPENJARVIS_NOTIFY_TITLE)) | Out-Null
    $nodes.Item(1).AppendChild($xml.CreateTextNode($env:OPENJARVIS_NOTIFY_MESSAGE)) | Out-Null
    # Windows PowerShell's own AppUserModelID: always registered, so the
    # toast shows without installing a shortcut for OpenJarvis.
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    $icon = New-Object System.Windows.Forms.NotifyIcon
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.Visible = $true
    $icon.ShowBalloonTip(10000, $env:OPENJARVIS_NOTIFY_TITLE, $env:OPENJARVIS_NOTIFY_MESSAGE, 'Info')
    Start-Sleep -Seconds 6
    $icon.Dispose()
}
"""


def _run(cmd: list[str], env: dict[str, str] | None = None) -> bool:
    try:
        result = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("Notification command %s failed: %s", cmd[0], exc)
        return False
    return result.returncode == 0


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str) -> bool:
    """Show a desktop notification. Returns False if none could be shown."""
    title = title.strip()[:120] or "OpenJarvis"
    message = message.strip()[:500]
    if sys.platform == "win32":
        encoded = base64.b64encode(_WINDOWS_TOAST.encode("utf-16-le")).decode()
        env = {
            **os.environ,
            "OPENJARVIS_NOTIFY_TITLE": title,
            "OPENJARVIS_NOTIFY_MESSAGE": message,
        }
        return _run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            env=env,
        )
    if sys.platform == "darwin":
        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)}"
        )
        return _run(["osascript", "-e", script])
    if shutil.which("notify-send"):
        return _run(["notify-send", "--app-name=OpenJarvis", title, message])
    return False


__all__ = ["notify"]
