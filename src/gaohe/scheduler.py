import getpass
import os
from collections.abc import Sequence
from pathlib import Path
import subprocess


class SchedulerRunner:
    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["schtasks.exe", *args], capture_output=True, text=True, shell=False, check=False,
        )


def _powershell_path() -> Path:
    return (Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe").resolve()


def _task_command(project_dir: Path, python_path: Path, env_path: Path) -> str:
    script_path = (project_dir / "scripts" / "run-watch.ps1").resolve()
    return (
        f'"{_powershell_path()}" -NoProfile -ExecutionPolicy Bypass -File "{script_path}" '
        f'-PythonPath "{python_path}" -EnvFile "{env_path}"'
    )


def install_task(
    task_name: str,
    project_dir: Path,
    python_path: Path,
    interval_minutes: int,
    runner: SchedulerRunner,
    env_path: Path | None = None,
) -> None:
    if interval_minutes <= 0:
        raise ValueError("interval must be positive")
    project_path = project_dir.resolve()
    interpreter = python_path.resolve()
    settings_path = (env_path or project_path / ".env").resolve()
    result = runner.run([
        "/Create", "/TN", task_name, "/TR", _task_command(project_path, interpreter, settings_path),
        "/SC", "MINUTE", "/MO", str(interval_minutes), "/RU", getpass.getuser(),
        "/IT", "/RL", "LIMITED", "/F",
    ])
    if result.returncode != 0:
        raise RuntimeError("scheduler unavailable")


def remove_task(task_name: str, runner: SchedulerRunner) -> None:
    if runner.run(["/Delete", "/TN", task_name, "/F"]).returncode != 0:
        raise RuntimeError("scheduler unavailable")


def task_status(task_name: str, runner: SchedulerRunner) -> str:
    result = runner.run(["/Query", "/TN", task_name])
    if result.returncode == 0:
        return "installed"
    message = f"{result.stdout}\n{result.stderr}".lower()
    if (
        "cannot find" in message
        or "not found" in message
        or "0x80070002" in message
        or "找不到" in message
        or "不存在" in message
    ):
        return "not installed"
    return "query failed"
