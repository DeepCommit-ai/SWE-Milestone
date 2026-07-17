from harness.e2e.agent_runner import E2EAgentRunner


def test_submission_command_uses_shell_paths_and_root_manifests():
    runner = object.__new__(E2EAgentRunner)
    runner.repo_src_dirs = ["core/", "gateway/"]
    runner.prompt_version = "v2"

    prompt = runner.generate_prompt()

    assert "git add core/ gateway/" in prompt
    assert "git add `core/`" not in prompt
    assert "go.mod go.sum go.work go.work.sum pom.xml" in prompt
    assert "git add -A -- \"$f\"" in prompt
    assert "{src_paths}" not in prompt
