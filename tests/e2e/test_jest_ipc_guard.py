"""Tests for the worker-side jest IPC guard (evaluator._install_jest_ipc_guard).

The guard exists because jest-runner uses `serialization: 'json'` for worker
IPC: a result carrying a cycle makes the worker's own process.send throw, jest
retries 4x, and the suite is reported with zero assertions — its tests vanish
from the report and score as non-achievements even though they ran.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from harness.e2e.evaluator import PatchEvaluator  # noqa: E402

ASSET = (
    Path(__file__).resolve().parents[2]
    / "harness" / "e2e" / "assets" / "jest_ipc_guard.js"
)


class TestGuardAsset:
    def test_asset_ships_with_the_harness(self):
        assert ASSET.exists(), "guard asset must be committed, not staged at runtime"

    def test_asset_is_valid_javascript(self):
        node = subprocess.run(["node", "--version"], capture_output=True)
        if node.returncode != 0:
            pytest.skip("node unavailable")
        res = subprocess.run(["node", "--check", str(ASSET)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

    def test_guard_is_inert_without_process_send(self):
        """Loaded in a parent process the guard must do nothing at all."""
        node = subprocess.run(["node", "--version"], capture_output=True)
        if node.returncode != 0:
            pytest.skip("node unavailable")
        res = subprocess.run(
            ["node", "--require", str(ASSET), "-e",
             "console.log(typeof process.send)"],
            capture_output=True, text=True,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "undefined"

    def test_guard_only_rewrites_unserializable_payloads(self):
        """A serializable message must pass through byte-identically."""
        node = subprocess.run(["node", "--version"], capture_output=True)
        if node.returncode != 0:
            pytest.skip("node unavailable")
        script = f"""
        const {{fork}} = require('child_process');
        const fs = require('fs');
        const child = fs.mkdtempSync('/tmp/guard') + '/c.js';
        fs.writeFileSync(child, `
          const clean = {{a: 1, b: [1, 2], c: 'x'}};
          process.send(clean);
          const cyc = {{name: 'n'}}; cyc.self = cyc;
          process.send({{payload: cyc, keep: 'kept'}});
          process.exit(0);
        `);
        const cp = fork(child, [], {{
          serialization: 'json',
          execArgv: ['--require', {json.dumps(str(ASSET))}],
          stdio: 'pipe',
        }});
        const got = [];
        cp.on('message', (m) => got.push(m));
        cp.on('exit', (code) => {{
          console.log(JSON.stringify({{code, got}}));
        }});
        """
        res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout.strip().splitlines()[-1])
        assert out["code"] == 0, "guarded child must not die on a cyclic payload"
        assert len(out["got"]) == 2, "both messages must arrive"
        # message 1: untouched
        assert out["got"][0] == {"a": 1, "b": [1, 2], "c": "x"}
        # message 2: cycle broken, siblings preserved
        assert out["got"][1]["keep"] == "kept"
        assert out["got"][1]["payload"]["name"] == "n"
        assert out["got"][1]["payload"]["self"] == "[Circular]"

    def test_unguarded_child_dies_on_the_same_payload(self):
        """Control: without the guard the cyclic send is fatal.

        This is the regression the guard exists for; if this ever passes
        cleanly, jest's IPC contract changed and the guard may be redundant.
        """
        node = subprocess.run(["node", "--version"], capture_output=True)
        if node.returncode != 0:
            pytest.skip("node unavailable")
        script = """
        const {fork} = require('child_process');
        const fs = require('fs');
        const child = fs.mkdtempSync('/tmp/noguard') + '/c.js';
        fs.writeFileSync(child, `
          const cyc = {name: 'n'}; cyc.self = cyc;
          process.send({payload: cyc});
          process.exit(0);
        `);
        const cp = fork(child, [], {serialization: 'json', stdio: 'pipe'});
        cp.on('exit', (code) => console.log(JSON.stringify({code})));
        """
        res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
        out = json.loads(res.stdout.strip().splitlines()[-1])
        assert out["code"] != 0, "unguarded cyclic send is expected to be fatal"


class TestExecEnvMerge:
    """_exec_env must append to the image's NODE_OPTIONS, never replace it."""

    def _bare(self) -> PatchEvaluator:
        return PatchEvaluator.__new__(PatchEvaluator)

    def test_missing_attributes_degrade_to_empty(self):
        # evaluators built in tests bypass __init__; this must not raise
        assert self._bare()._exec_env() == {}

    def test_extra_env_is_merged_with_go_env(self):
        ev = self._bare()
        ev._extra_exec_env = {"NODE_OPTIONS": "--max-old-space-size=4096 --require=/g.js"}
        ev._go_exec_env = {"GOFLAGS": "-mod=mod"}
        env = ev._exec_env()
        assert env["GOFLAGS"] == "-mod=mod"
        assert "--max-old-space-size=4096" in env["NODE_OPTIONS"]
        assert "--require=/g.js" in env["NODE_OPTIONS"]

    def test_go_env_wins_on_key_collision(self):
        ev = self._bare()
        ev._extra_exec_env = {"GOFLAGS": "generic"}
        ev._go_exec_env = {"GOFLAGS": "go-specific"}
        assert ev._exec_env()["GOFLAGS"] == "go-specific"

    def test_guard_path_is_outside_testbed(self):
        """The guard must never live under /testbed — git clean would remove it
        and it would pollute the agent's tree."""
        assert not PatchEvaluator.JEST_IPC_GUARD_PATH.startswith("/testbed")
