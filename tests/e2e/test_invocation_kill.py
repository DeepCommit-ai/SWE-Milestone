"""Tests for issue #19: agent timeouts must kill the in-container invocation.

Killing the host-side ``docker exec`` client (``proc.kill()`` /
``subprocess.run(timeout=...)``) only severs the stream — the payload keeps
running inside the container, and recovery then starts a second agent against
the same /testbed and session.

The fix plants two identities per invocation: an exported
EVOCLAW_INVOCATION_ID (the kill script's authoritative selector — inherited
by daemonized descendants and TERM-window forks, both invisible to a parent
walk) and a pidfile (root for a supplementary /proc BFS covering
env-scrubbing children). On timeout the script TERMs, escalates to KILL, and
verifies death before recovery is allowed; unverifiable kills fail closed
through _last_fatal_error.

Unit tests exercise the host-side wiring with mocked subprocess calls; the
integration tests at the bottom run the real kill script inside real
containers (debian + busybox bases) and are skipped when docker is missing.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from harness.e2e.agent_runner import _KILL_INVOCATION_SCRIPT, AgentRunner

INVID = "cafe0123beef"


def _make_runner() -> AgentRunner:
    return AgentRunner(container_name="dummy-container", agent_name="claude-code")


def _arm(runner: AgentRunner) -> None:
    """Give the runner a live invocation identity (as _wrap_with_pidfile would)."""
    runner._invocation_id = INVID
    runner._invocation_pidfile = f"/tmp/evoclaw_invocation_{INVID}.pid"


# ---------------------------------------------------------------------------
# _wrap_with_pidfile
# ---------------------------------------------------------------------------


def test_wrap_preserves_command_and_plants_both_identities():
    runner = _make_runner()
    cmd = 'FOO=bar claude --model x < /tmp/agent_prompt.txt && echo done'
    wrapped = runner._wrap_with_pidfile(cmd)

    assert runner._invocation_id and len(runner._invocation_id) == 12
    assert runner._invocation_pidfile == f"/tmp/evoclaw_invocation_{runner._invocation_id}.pid"
    # the original command must survive verbatim after the prefix
    assert wrapped.endswith("; " + cmd)
    # env marker is exported so every descendant inherits it
    assert wrapped.startswith(f"EVOCLAW_INVOCATION_ID={runner._invocation_id}; export EVOCLAW_INVOCATION_ID; ")
    assert f'echo $$ >"{runner._invocation_pidfile}"' in wrapped
    # no exec: compound agent commands (&&, env prefixes) must keep running
    assert "exec " not in wrapped[: len(wrapped) - len(cmd)]


def test_wrap_generates_unique_identities():
    runner = _make_runner()
    runner._wrap_with_pidfile("a")
    first = (runner._invocation_id, runner._invocation_pidfile)
    runner._wrap_with_pidfile("b")
    assert (runner._invocation_id, runner._invocation_pidfile) != first


def test_run_applies_wrap_to_executed_command(tmp_path):
    """Review gap: nothing asserted the wrap actually reaches the docker exec
    command — deleting the call site left the whole suite green."""
    runner = AgentRunner(container_name="dummy", agent_name="claude-code", log_dir=tmp_path)
    captured = {}

    def fake_stream(docker_exec_cmd, prompt_path):
        captured["cmd"] = docker_exec_cmd
        return True

    with patch.object(runner, "_execute_with_streaming", side_effect=fake_stream), patch.object(
        runner, "_run_command"
    ), patch.object(runner, "_update_session_id_from_output"):
        ok, _ = runner.run("prompt")

    assert ok is True
    payload = captured["cmd"][-1]
    assert payload.startswith("EVOCLAW_INVOCATION_ID=")
    assert 'echo $$ >"/tmp/evoclaw_invocation_' in payload


def test_resume_applies_wrap_to_executed_command(tmp_path):
    runner = AgentRunner(container_name="dummy", agent_name="claude-code", log_dir=tmp_path)
    captured = {}

    def fake_run(cmd, **kwargs):
        if cmd[0] == "docker" and "exec" in cmd and kwargs.get("timeout") is not None:
            captured["cmd"] = cmd
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    with patch("harness.e2e.agent_runner.subprocess.run", side_effect=fake_run), patch.object(
        runner, "_run_command"
    ), patch.object(runner, "_update_session_id_from_output"):
        ok = runner.resume_session("sess-1", "continue", timeout_ms=1000)

    assert ok is True
    payload = captured["cmd"][-1]
    assert payload.startswith("EVOCLAW_INVOCATION_ID=")


# ---------------------------------------------------------------------------
# _kill_container_invocation result handling (mocked docker)
# ---------------------------------------------------------------------------


def _completed(stdout: str = "", rc: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=rc, stderr=stderr)


@pytest.mark.parametrize("marker", ["RESULT:KILLED", "RESULT:ALREADY_DEAD"])
def test_kill_verified_dead_markers_return_true(marker):
    runner = _make_runner()
    _arm(runner)
    with patch("harness.e2e.agent_runner.subprocess.run", return_value=_completed(marker + "\n")) as mocked:
        assert runner._kill_container_invocation("test") is True
    # both identity references are consumed so a later call cannot re-kill
    assert runner._invocation_pidfile is None
    assert runner._invocation_id is None
    # the invocation id is passed to the script (env selector argument)
    kill_cmd = mocked.call_args_list[0].args[0]
    assert INVID in kill_cmd


def test_kill_survivors_return_false():
    runner = _make_runner()
    _arm(runner)
    with patch(
        "harness.e2e.agent_runner.subprocess.run",
        return_value=_completed("RESULT:SURVIVORS 42 43\n", rc=1),
    ):
        assert runner._kill_container_invocation("test") is False


def test_kill_without_identity_is_noop_true():
    runner = _make_runner()
    runner._invocation_pidfile = None
    runner._invocation_id = None
    with patch("harness.e2e.agent_runner.subprocess.run") as mocked:
        assert runner._kill_container_invocation("test") is True
    mocked.assert_not_called()


def test_kill_exec_failure_container_stopped_counts_dead():
    runner = _make_runner()
    _arm(runner)

    def fake_run(cmd, **kwargs):
        if "inspect" in cmd:
            return _completed("false\n")
        return _completed("", rc=1, stderr="Error response from daemon")

    with patch("harness.e2e.agent_runner.subprocess.run", side_effect=fake_run):
        assert runner._kill_container_invocation("test") is True


def test_kill_exec_failure_container_removed_counts_dead():
    runner = _make_runner()
    _arm(runner)

    def fake_run(cmd, **kwargs):
        if "inspect" in cmd:
            return _completed("", rc=1, stderr="Error: No such object: dummy-container")
        return _completed("", rc=1, stderr="Error response from daemon")

    with patch("harness.e2e.agent_runner.subprocess.run", side_effect=fake_run):
        assert runner._kill_container_invocation("test") is True


def test_kill_exec_failure_container_running_fails_closed():
    runner = _make_runner()
    _arm(runner)

    def fake_run(cmd, **kwargs):
        if "inspect" in cmd:
            return _completed("true\n")
        return _completed("", rc=1, stderr="transient daemon error")

    with patch("harness.e2e.agent_runner.subprocess.run", side_effect=fake_run):
        # container running but death unverified: must NOT clear the way
        assert runner._kill_container_invocation("test") is False


def test_kill_script_timeout_fails_closed():
    runner = _make_runner()
    _arm(runner)
    with patch(
        "harness.e2e.agent_runner.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=120),
    ):
        assert runner._kill_container_invocation("test") is False


def test_inspect_timeout_fails_closed():
    """Review finding: the inspect fallback runs precisely when the daemon is
    misbehaving; without its own timeout it hung the whole trial."""
    runner = _make_runner()
    _arm(runner)

    def fake_run(cmd, **kwargs):
        if "inspect" in cmd:
            raise subprocess.TimeoutExpired(cmd="docker inspect", timeout=60)
        return _completed("", rc=1, stderr="daemon wedged")

    with patch("harness.e2e.agent_runner.subprocess.run", side_effect=fake_run):
        assert runner._kill_container_invocation("test") is False


# ---------------------------------------------------------------------------
# timeout paths set the fatal flag when the kill cannot be verified
# ---------------------------------------------------------------------------


class _FakePipe:
    def readline(self):
        return ""

    def close(self):
        pass


class _TimeoutProc:
    stdout = _FakePipe()
    stderr = _FakePipe()
    returncode = -9

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=timeout)

    def kill(self):
        pass


def test_run_timeout_sets_fatal_when_survivors_remain(tmp_path):
    runner = AgentRunner(container_name="dummy", agent_name="claude-code", log_dir=tmp_path, timeout_ms=10)

    with patch("harness.e2e.agent_runner.subprocess.Popen", return_value=_TimeoutProc()), patch.object(
        runner, "_run_command"
    ), patch.object(runner, "_kill_container_invocation", return_value=False) as killer:
        ok = runner._execute_with_streaming(["docker", "exec", "dummy"], "/tmp/p.txt")

    assert ok is False
    killer.assert_called_once()
    assert runner._last_fatal_error is not None
    assert "#19" in runner._last_fatal_error


def test_run_timeout_no_fatal_when_kill_verified(tmp_path):
    runner = AgentRunner(container_name="dummy", agent_name="claude-code", log_dir=tmp_path, timeout_ms=10)

    with patch("harness.e2e.agent_runner.subprocess.Popen", return_value=_TimeoutProc()), patch.object(
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


def test_broad_except_preserves_fatal_verdict(tmp_path):
    """Review finding: run()'s broad except reset _last_fatal_error, so an
    unrelated exception after a failed-kill timeout (e.g. session-id
    extraction) erased the fatal verdict and recovery ran concurrently."""
    runner = AgentRunner(container_name="dummy", agent_name="claude-code", log_dir=tmp_path, timeout_ms=10)

    def stream_timeout_with_fatal(docker_exec_cmd, prompt_path):
        runner._last_fatal_error = "survivors remain (#19)"
        return False

    with patch.object(runner, "_execute_with_streaming", side_effect=stream_timeout_with_fatal), patch.object(
        runner, "_update_session_id_from_output", side_effect=MemoryError("giant stdout")
    ), patch.object(runner, "_run_command"):
        ok, _ = runner.run("prompt")

    assert ok is False
    assert runner._last_fatal_error == "survivors remain (#19)"


# ---------------------------------------------------------------------------
# run_milestone honors the fatal verdict (review finding: it never read it)
# ---------------------------------------------------------------------------


def test_run_milestone_retry_loop_reads_fatal_flag():
    """The ITE retry loop must abort on _last_fatal_error instead of
    launching another agent beside an unkilled survivor."""
    import inspect

    from harness.e2e import run_milestone

    src = inspect.getsource(run_milestone)
    assert "_last_fatal_error" in src, "run_milestone.py must consume the fatal verdict (#19)"
    # the check must abort the retry loop, not merely log
    marker = src.index("_last_fatal_error")
    assert "break" in src[marker : marker + 1200]


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
    # Production parity: the payload runs as fakeroot and the kill execs as
    # fakeroot too (same-uid is what makes /proc/PID/environ readable without
    # CAP_SYS_PTRACE, which docker drops). busybox has adduser, debian has
    # useradd.
    add_user = "adduser -D fakeroot" if "busybox" in image else "useradd -m fakeroot"
    subprocess.run(["docker", "exec", name, "/bin/sh", "-c", add_user], check=True, capture_output=True)
    yield name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _launch_fake_invocation(container: str, agent_cmd: str, invid: str = INVID) -> None:
    """Launch a detached payload exactly as the harness wrapper would
    (same user as production: fakeroot)."""
    payload = (
        f"EVOCLAW_INVOCATION_ID={invid}; export EVOCLAW_INVOCATION_ID; "
        f'echo $$ >"/tmp/evoclaw_invocation_{invid}.pid"; {agent_cmd}'
    )
    subprocess.run(
        ["docker", "exec", "-d", "--user", "fakeroot", container, "/bin/sh", "-c", payload],
        check=True,
        capture_output=True,
    )
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
    _arm(runner)
    _launch_fake_invocation(throwaway_container, "sleep 543 & sleep 543 & wait")
    # >= 2: the parent sh's own cmdline also contains the sentinel string
    assert _pids_matching(throwaway_container, "sleep 543") >= 2

    assert runner._kill_container_invocation("integration", grace_seconds=3) is True
    assert _pids_matching(throwaway_container, "sleep 543") == 0


@pytest.mark.skipif(not DOCKER, reason="docker unavailable")
@pytest.mark.parametrize(
    "throwaway_container", ["debian:bookworm-slim", "busybox:latest"], indirect=True
)
def test_kill_daemonized_descendant(throwaway_container):
    """Review finding (reproduced): a `( child & )` whose intermediate shell
    exits gets reparented to PID 1 and escapes any parent-walk. The env
    selector must find and kill it anyway."""
    runner = AgentRunner(container_name=throwaway_container, agent_name="claude-code")
    _arm(runner)
    _launch_fake_invocation(throwaway_container, "(sleep 391 &); sleep 543 & wait")
    assert _pids_matching(throwaway_container, "sleep 391") >= 1

    assert runner._kill_container_invocation("integration", grace_seconds=3) is True
    assert _pids_matching(throwaway_container, "sleep 391") == 0
    assert _pids_matching(throwaway_container, "sleep 543") == 0


@pytest.mark.skipif(not DOCKER, reason="docker unavailable")
@pytest.mark.parametrize("throwaway_container", ["debian:bookworm-slim"], indirect=True)
def test_kill_live_tree_with_lost_pidfile(throwaway_container):
    """Review finding (reproduced): a live tree whose pidfile was deleted
    (agent /tmp cleanup, ENOSPC) previously produced a fail-open NO_PIDFILE.
    The env selector must still find and kill everything."""
    runner = AgentRunner(container_name=throwaway_container, agent_name="claude-code")
    _arm(runner)
    _launch_fake_invocation(throwaway_container, "sleep 543 & sleep 543 & wait")
    subprocess.run(
        ["docker", "exec", throwaway_container, "rm", "-f", f"/tmp/evoclaw_invocation_{INVID}.pid"],
        check=True, capture_output=True,
    )

    assert runner._kill_container_invocation("integration", grace_seconds=3) is True
    assert _pids_matching(throwaway_container, "sleep 543") == 0


@pytest.mark.skipif(not DOCKER, reason="docker unavailable")
@pytest.mark.parametrize("throwaway_container", ["debian:bookworm-slim"], indirect=True)
def test_kill_term_window_fork(throwaway_container):
    """A child forked from a TERM trap still carries the env marker; the
    pre-KILL rescan must include it."""
    runner = AgentRunner(container_name=throwaway_container, agent_name="claude-code")
    _arm(runner)
    _launch_fake_invocation(
        throwaway_container,
        "/bin/sh -c 'trap \"sleep 387 & exit 0\" TERM; sleep 543 & wait' & wait",
    )
    assert _pids_matching(throwaway_container, "sleep 543") >= 1

    assert runner._kill_container_invocation("integration", grace_seconds=3) is True
    assert _pids_matching(throwaway_container, "sleep 387") == 0
    assert _pids_matching(throwaway_container, "sleep 543") == 0


@pytest.mark.skipif(not DOCKER, reason="docker unavailable")
@pytest.mark.parametrize("throwaway_container", ["debian:bookworm-slim"], indirect=True)
def test_kill_term_immune_child_escalates(throwaway_container):
    """A child that traps TERM is taken down by the KILL escalation."""
    runner = AgentRunner(container_name=throwaway_container, agent_name="claude-code")
    _arm(runner)
    _launch_fake_invocation(
        throwaway_container,
        '/bin/sh -c \'trap "" TERM; sleep 543\' & wait',
    )
    assert _pids_matching(throwaway_container, "sleep 543") >= 1

    assert runner._kill_container_invocation("integration", grace_seconds=2) is True
    assert _pids_matching(throwaway_container, "sleep 543") == 0


@pytest.mark.skipif(not DOCKER, reason="docker unavailable")
@pytest.mark.parametrize("throwaway_container", ["debian:bookworm-slim"], indirect=True)
def test_kill_already_finished_invocation_is_clean(throwaway_container):
    runner = AgentRunner(container_name=throwaway_container, agent_name="claude-code")
    _arm(runner)
    subprocess.run(
        ["docker", "exec", throwaway_container, "/bin/sh", "-c",
         f"echo 99999 >/tmp/evoclaw_invocation_{INVID}.pid"],
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
