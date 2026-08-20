"""Tests for issue #19: agent timeouts must kill the in-container invocation.

Killing the host-side ``docker exec`` client (``proc.kill()`` /
``subprocess.run(timeout=...)``) only severs the stream — the payload keeps
running inside the container, and recovery then starts a second agent against
the same /testbed and session. The fix wraps every invocation with a pidfile
prefix and, on timeout, kills the pidfile PID's descendant tree and verifies
death before recovery is allowed.

Unit tests exercise the host-side wiring with mocked subprocess calls; the
integration tests at the bottom run the real kill script inside real
containers (debian + busybox bases) and are skipped when docker is missing.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness.e2e.agent_runner import _KILL_INVOCATION_SCRIPT, AgentRunner


def _make_runner() -> AgentRunner:
    return AgentRunner(container_name="dummy-container", agent_name="claude-code")


# ---------------------------------------------------------------------------
# _wrap_with_pidfile
# ---------------------------------------------------------------------------


def test_wrap_preserves_command_and_records_pidfile():
    runner = _make_runner()
    cmd = 'FOO=bar claude --model x < /tmp/agent_prompt.txt && echo done'
    wrapped = runner._wrap_with_pidfile(cmd)

    assert runner._invocation_pidfile is not None
    assert runner._invocation_pidfile.startswith("/tmp/evoclaw_invocation_")
    # the original command must survive verbatim after the prefix
    assert wrapped.endswith("; " + cmd)
    assert wrapped.startswith(f'echo $$ >"{runner._invocation_pidfile}"')
    # no exec: compound agent commands (&&, env prefixes) must keep running
    assert "exec " not in wrapped[: len(wrapped) - len(cmd)]


def test_wrap_generates_unique_pidfiles():
    runner = _make_runner()
    runner._wrap_with_pidfile("a")
    first = runner._invocation_pidfile
    runner._wrap_with_pidfile("b")
    assert runner._invocation_pidfile != first


# ---------------------------------------------------------------------------
# _kill_container_invocation result handling (mocked docker)
# ---------------------------------------------------------------------------


def _completed(stdout: str = "", rc: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=rc, stderr=stderr)


@pytest.mark.parametrize(
    "marker",
    ["RESULT:KILLED", "RESULT:ALREADY_DEAD", "RESULT:NO_PIDFILE", "RESULT:BAD_PIDFILE"],
)
def test_kill_verified_dead_markers_return_true(marker):
    runner = _make_runner()
    runner._invocation_pidfile = "/tmp/evoclaw_invocation_test.pid"
    with patch("harness.e2e.agent_runner.subprocess.run", return_value=_completed(marker + "\n")):
        assert runner._kill_container_invocation("test") is True
    # pidfile reference is consumed so a later call cannot re-kill
    assert runner._invocation_pidfile is None


def test_kill_survivors_return_false():
    runner = _make_runner()
    runner._invocation_pidfile = "/tmp/evoclaw_invocation_test.pid"
    with patch(
        "harness.e2e.agent_runner.subprocess.run",
        return_value=_completed("RESULT:SURVIVORS 42 43\n", rc=1),
    ):
        assert runner._kill_container_invocation("test") is False


def test_kill_without_pidfile_is_noop_true():
    runner = _make_runner()
    runner._invocation_pidfile = None
    with patch("harness.e2e.agent_runner.subprocess.run") as mocked:
        assert runner._kill_container_invocation("test") is True
    mocked.assert_not_called()


def test_kill_exec_failure_container_stopped_counts_dead():
    runner = _make_runner()
    runner._invocation_pidfile = "/tmp/evoclaw_invocation_test.pid"

    def fake_run(cmd, **kwargs):
        if "inspect" in cmd:
            return _completed("false\n")
        return _completed("", rc=1, stderr="Error response from daemon")

    with patch("harness.e2e.agent_runner.subprocess.run", side_effect=fake_run):
        assert runner._kill_container_invocation("test") is True


def test_kill_exec_failure_container_removed_counts_dead():
    runner = _make_runner()
    runner._invocation_pidfile = "/tmp/evoclaw_invocation_test.pid"

    def fake_run(cmd, **kwargs):
        if "inspect" in cmd:
            return _completed("", rc=1, stderr="Error: No such object: dummy-container")
        return _completed("", rc=1, stderr="Error response from daemon")

    with patch("harness.e2e.agent_runner.subprocess.run", side_effect=fake_run):
        assert runner._kill_container_invocation("test") is True


def test_kill_exec_failure_container_running_fails_closed():
    runner = _make_runner()
    runner._invocation_pidfile = "/tmp/evoclaw_invocation_test.pid"

    def fake_run(cmd, **kwargs):
        if "inspect" in cmd:
            return _completed("true\n")
        return _completed("", rc=1, stderr="transient daemon error")

    with patch("harness.e2e.agent_runner.subprocess.run", side_effect=fake_run):
        # container running but death unverified: must NOT clear the way
        assert runner._kill_container_invocation("test") is False


def test_kill_script_timeout_fails_closed():
    runner = _make_runner()
    runner._invocation_pidfile = "/tmp/evoclaw_invocation_test.pid"
    with patch(
        "harness.e2e.agent_runner.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=120),
    ):
        assert runner._kill_container_invocation("test") is False


# ---------------------------------------------------------------------------
# timeout paths set the fatal flag when the kill cannot be verified
# ---------------------------------------------------------------------------


def test_run_timeout_sets_fatal_when_survivors_remain(tmp_path):
    runner = AgentRunner(container_name="dummy", agent_name="claude-code", log_dir=tmp_path, timeout_ms=10)

    class FakePipe:
        def readline(self):
            return ""

        def close(self):
            pass

    class FakeProc:
        stdout = FakePipe()
        stderr = FakePipe()
        returncode = -9

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="docker", timeout=timeout)

        def kill(self):
            pass

    with patch("harness.e2e.agent_runner.subprocess.Popen", return_value=FakeProc()), patch.object(
        runner, "_run_command"
    ), patch.object(runner, "_kill_container_invocation", return_value=False) as killer:
        ok = runner._execute_with_streaming(["docker", "exec", "dummy"], "/tmp/p.txt")

    assert ok is False
    killer.assert_called_once()
    assert runner._last_fatal_error is not None
    assert "#19" in runner._last_fatal_error


def test_run_timeout_no_fatal_when_kill_verified(tmp_path):
    runner = AgentRunner(container_name="dummy", agent_name="claude-code", log_dir=tmp_path, timeout_ms=10)

    class FakePipe:
        def readline(self):
            return ""

        def close(self):
            pass

    class FakeProc:
        stdout = FakePipe()
        stderr = FakePipe()
        returncode = -9

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="docker", timeout=timeout)

        def kill(self):
            pass

    with patch("harness.e2e.agent_runner.subprocess.Popen", return_value=FakeProc()), patch.object(
        runner, "_run_command"
    ), patch.object(runner, "_kill_container_invocation", return_value=True):
        ok = runner._execute_with_streaming(["docker", "exec", "dummy"], "/tmp/p.txt")

    assert ok is False
    assert runner._last_fatal_error is None


def test_run_timeout_without_log_dir_invokes_kill():
    """The no-streaming shortcut (log_dir=None) must honor the same contract:
    before the fix its TimeoutExpired escaped to run()'s broad except, which
    logged and reset the fatal flag without ever killing the payload."""
    runner = AgentRunner(container_name="dummy", agent_name="claude-code", log_dir=None, timeout_ms=10)

    with patch(
        "harness.e2e.agent_runner.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=0.01),
    ), patch.object(runner, "_run_command"), patch.object(
        runner, "_kill_container_invocation", return_value=False
    ) as killer:
        ok = runner._execute_with_streaming(["docker", "exec", "dummy"], "/tmp/p.txt")

    assert ok is False
    killer.assert_called_once()
    assert runner._last_fatal_error is not None


def test_resume_timeout_invokes_kill(tmp_path):
    runner = AgentRunner(container_name="dummy", agent_name="claude-code", log_dir=tmp_path)

    def fake_run(cmd, **kwargs):
        if kwargs.get("timeout") is not None and cmd[0] == "docker" and "exec" in cmd:
            raise subprocess.TimeoutExpired(cmd="docker", timeout=kwargs["timeout"])
        return _completed("")

    with patch("harness.e2e.agent_runner.subprocess.run", side_effect=fake_run), patch.object(
        runner, "_run_command"
    ), patch.object(runner, "_kill_container_invocation", return_value=False) as killer:
        ok = runner.resume_session("sess-1", "continue", timeout_ms=10)

    assert ok is False
    killer.assert_called_once()
    assert runner._last_fatal_error is not None


# ---------------------------------------------------------------------------
# integration: real containers, real kill script
# ---------------------------------------------------------------------------

DOCKER = shutil.which("docker") is not None


def _image_available(image: str) -> bool:
    if not DOCKER:
        return False
    r = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    return r.returncode == 0


@pytest.fixture
def throwaway_container(request):
    image = request.param
    if not _image_available(image):
        pytest.skip(f"image {image} not present locally")
    name = f"invkill_test_{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--name", name, image, "sleep", "600"], check=True, capture_output=True)
    yield name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _launch_fake_invocation(container: str, payload: str) -> None:
    subprocess.run(
        ["docker", "exec", "-d", container, "/bin/sh", "-c", payload],
        check=True,
        capture_output=True,
    )
    import time

    time.sleep(2)


def _pids_matching(container: str, needle: str) -> int:
    r = subprocess.run(
        [
            "docker", "exec", container, "/bin/sh", "-c",
            'for d in /proc/[0-9]*/cmdline; do tr "\\0" " " <"$d" 2>/dev/null; echo; done',
        ],
        capture_output=True,
        text=True,
    )
    return sum(1 for line in r.stdout.splitlines() if needle in line)


@pytest.mark.skipif(not DOCKER, reason="docker unavailable")
@pytest.mark.parametrize(
    "throwaway_container", ["debian:bookworm-slim", "busybox:latest"], indirect=True
)
def test_kill_real_process_tree(throwaway_container):
    """A detached sh + children tree is fully killed and verified dead."""
    runner = AgentRunner(container_name=throwaway_container, agent_name="claude-code")
    runner._invocation_pidfile = "/tmp/evoclaw_invocation_it.pid"
    _launch_fake_invocation(
        throwaway_container,
        'echo $$ >/tmp/evoclaw_invocation_it.pid; sleep 543 & sleep 543 & wait',
    )
    # >= 2: the parent sh's own cmdline also contains the sentinel string
    assert _pids_matching(throwaway_container, "sleep 543") >= 2

    assert runner._kill_container_invocation("integration", grace_seconds=3) is True
    assert _pids_matching(throwaway_container, "sleep 543") == 0


@pytest.mark.skipif(not DOCKER, reason="docker unavailable")
@pytest.mark.parametrize("throwaway_container", ["debian:bookworm-slim"], indirect=True)
def test_kill_term_immune_child_escalates(throwaway_container):
    """A child that traps TERM is taken down by the KILL escalation."""
    runner = AgentRunner(container_name=throwaway_container, agent_name="claude-code")
    runner._invocation_pidfile = "/tmp/evoclaw_invocation_it.pid"
    _launch_fake_invocation(
        throwaway_container,
        'echo $$ >/tmp/evoclaw_invocation_it.pid; /bin/sh -c \'trap "" TERM; sleep 543\' & wait',
    )
    assert _pids_matching(throwaway_container, "sleep 543") >= 1

    assert runner._kill_container_invocation("integration", grace_seconds=2) is True
    assert _pids_matching(throwaway_container, "sleep 543") == 0


@pytest.mark.skipif(not DOCKER, reason="docker unavailable")
@pytest.mark.parametrize("throwaway_container", ["debian:bookworm-slim"], indirect=True)
def test_kill_already_finished_invocation_is_clean(throwaway_container):
    runner = AgentRunner(container_name=throwaway_container, agent_name="claude-code")
    runner._invocation_pidfile = "/tmp/evoclaw_invocation_it.pid"
    subprocess.run(
        ["docker", "exec", throwaway_container, "/bin/sh", "-c", "echo 99999 >/tmp/evoclaw_invocation_it.pid"],
        check=True,
        capture_output=True,
    )
    assert runner._kill_container_invocation("integration") is True


def test_kill_script_is_posix_shell_clean():
    """`sh -n` accepts the embedded script (host-side smoke, no container)."""
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh on host")
    r = subprocess.run([sh, "-n", "-c", _KILL_INVOCATION_SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
