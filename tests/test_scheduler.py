from pathlib import Path
from subprocess import CompletedProcess

import pytest

import gaohe.cli as cli
import gaohe.scheduler as scheduler
from gaohe.scheduler import SchedulerRunner, install_task, remove_task, task_status


class FakeRunner:
    def __init__(self, *results: CompletedProcess[str]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def run(self, args):
        self.calls.append(list(args))
        return self.results.pop(0)


def result(code: int = 0, stdout: str = "", stderr: str = "") -> CompletedProcess[str]:
    return CompletedProcess(["schtasks.exe"], code, stdout, stderr)


def test_install_uses_stable_user_task_with_absolute_runner_paths(tmp_path):
    runner = FakeRunner(result(), result())
    project = tmp_path / "project"
    python = project / ".venv" / "Scripts" / "python.exe"

    install_task("GaoHe Watch", project, python, 15, runner)
    install_task("GaoHe Watch", project, python, 15, runner)

    assert runner.calls[0] == runner.calls[1]
    args = runner.calls[0]
    assert args[:2] == ["/Create", "/TN"]
    assert args[2] == "GaoHe Watch"
    assert args[args.index("/SC") + 1:args.index("/SC") + 4] == ["MINUTE", "/MO", "15"]
    assert "/IT" in args and "/F" in args and "/RU" in args
    command = args[args.index("/TR") + 1]
    assert str((project / "scripts" / "run-watch.ps1").resolve()) in command
    assert str(python.resolve()) in command
    assert "powershell.exe" in command.lower()


@pytest.mark.parametrize("interval", [0, -1])
def test_install_rejects_nonpositive_interval_without_invoking_runner(tmp_path, interval):
    runner = FakeRunner()

    with pytest.raises(ValueError, match="positive"):
        install_task("GaoHe Watch", tmp_path, tmp_path / "python.exe", interval, runner)

    assert runner.calls == []


def test_install_and_remove_fail_without_exposing_command_details(tmp_path):
    failed = FakeRunner(result(1, "private-stdout", "private-stderr"))

    with pytest.raises(RuntimeError, match="scheduler unavailable"):
        install_task("GaoHe Watch", tmp_path, tmp_path / "python.exe", 10, failed)
    with pytest.raises(RuntimeError, match="scheduler unavailable"):
        remove_task("GaoHe Watch", FakeRunner(result(1, "private", "private")))


def test_remove_and_status_have_safe_result_states():
    remove = FakeRunner(result())
    remove_task("GaoHe Watch", remove)
    assert remove.calls == [["/Delete", "/TN", "GaoHe Watch", "/F"]]

    assert task_status("GaoHe Watch", FakeRunner(result())) == "installed"
    assert task_status("GaoHe Watch", FakeRunner(result(1, stderr="ERROR: The system cannot find the file specified."))) == "not installed"
    assert task_status("GaoHe Watch", FakeRunner(result(1, stderr="Access is denied."))) == "query failed"


def test_scheduler_runner_uses_argument_list_without_shell(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return result()

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    SchedulerRunner().run(["/Query", "/TN", "GaoHe Watch"])

    assert captured["args"] == (["schtasks.exe", "/Query", "/TN", "GaoHe Watch"],)
    assert captured["kwargs"] == {"capture_output": True, "text": True, "shell": False, "check": False}


def test_run_watch_script_orders_analysis_after_watch_and_keeps_watch_exit_path():
    script = (Path(__file__).parents[1] / "scripts" / "run-watch.ps1").read_text(encoding="utf-8")

    assert script.index("watch --once") < script.index("analyze --pending")
    assert "$watchExit = $LASTEXITCODE" in script
    assert "if ($watchExit -ne 0)" in script
    assert "exit $watchExit" in script


def test_cli_schedule_commands_use_scheduler_runner_without_auto_install(tmp_path, monkeypatch, capsys):
    runner = FakeRunner(result(), result(), result())
    monkeypatch.setattr(cli, "SchedulerRunner", lambda: runner)
    env_file = tmp_path / ".env"
    env_file.write_text("POLL_INTERVAL_MINUTES=12\n", encoding="utf-8")

    assert cli.main(["schedule", "install", "--env-file", str(env_file), "--project-dir", str(tmp_path), "--python-path", str(tmp_path / "python.exe")]) == 0
    assert cli.main(["schedule", "status"]) == 0
    assert cli.main(["schedule", "remove"]) == 0

    assert capsys.readouterr().out.splitlines() == ["schedule installed", "installed", "schedule removed"]
    assert runner.calls[0][runner.calls[0].index("/MO") + 1] == "12"
