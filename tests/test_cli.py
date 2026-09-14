from gaohe.cli import main


def test_version_command_prints_package_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == "gaohe 0.1.0\n"
