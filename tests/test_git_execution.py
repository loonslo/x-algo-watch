import os
import subprocess
from types import SimpleNamespace

import pytest

from x_algo_watch import git
from x_algo_watch.models import WatchError


@pytest.mark.skipif(os.name != 'nt', reason='Git for Windows launcher layout')
@pytest.mark.parametrize('native_exists', [True, False])
def test_windows_git_uses_native_process_when_available(tmp_path, monkeypatch, native_exists):
    launcher = tmp_path / 'Git/cmd/git.exe'
    native = tmp_path / 'Git/mingw64/bin/git.exe'
    launcher.parent.mkdir(parents=True)
    launcher.touch()
    if native_exists:
        native.parent.mkdir(parents=True)
        native.touch()
    monkeypatch.setattr(git.shutil, 'which', lambda name: str(launcher))
    def run(args, **kwargs):
        assert args[0] == str(native if native_exists else launcher)
        assert kwargs['timeout'] == 120
        assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
        return SimpleNamespace(returncode=0, stdout=b'ok')
    monkeypatch.setattr(git.subprocess, 'run', run)
    assert git.run_git(['status']) == b'ok'


def test_git_timeout_is_incomplete_and_does_not_expose_command(monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(['UNTRUSTED_ARGUMENT'], 120)
    monkeypatch.setattr(git.subprocess, 'run', run)
    with pytest.raises(WatchError, match='本次未检查') as error:
        git.run_git(['status'])
    assert 'UNTRUSTED_ARGUMENT' not in str(error.value)
