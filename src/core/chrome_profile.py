import json
import os
import subprocess
from typing import List


def close_profile_chrome_windows(user_data_dir: str) -> int:
    if os.name != "nt" or not user_data_dir:
        return 0

    pids = _find_profile_root_pids(user_data_dir)
    closed = 0
    for pid in pids:
        if _taskkill(pid, force=False):
            closed += 1

    remaining = _find_profile_root_pids(user_data_dir)
    for pid in remaining:
        if _taskkill(pid, force=True):
            closed += 1
    return closed


def _find_profile_root_pids(user_data_dir: str) -> List[int]:
    escaped_dir = user_data_dir.replace("'", "''")
    script = f"""
$pattern = '*{escaped_dir}*'
$items = @(
  Get-CimInstance Win32_Process |
  Where-Object {{
    $_.Name -ieq 'chrome.exe' -and
    $_.CommandLine -like $pattern -and
    $_.CommandLine -notlike '*--type=*'
  }} |
  Select-Object -ExpandProperty ProcessId
)
$items | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return []

    raw = (result.stdout or "").strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    if isinstance(data, int):
        return [data]
    if isinstance(data, list):
        return [int(item) for item in data if str(item).isdigit()]
    return []


def _taskkill(pid: int, force: bool) -> bool:
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode == 0
