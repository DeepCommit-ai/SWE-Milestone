"""Focused tests for the Codex agent adapter."""

from harness.e2e.agents.codex import CodexFramework


def test_codex_oauth_reports_chatgpt_network_endpoint(monkeypatch):
    monkeypatch.delenv("UNIFIED_API_KEY", raising=False)
    monkeypatch.delenv("UNIFIED_BASE_URL", raising=False)

    framework = CodexFramework()

    assert framework.get_network_endpoint_url() == framework.OAUTH_ENDPOINT_URL


def test_codex_explicit_base_url_is_network_endpoint(monkeypatch):
    monkeypatch.setenv("UNIFIED_API_KEY", "test-key")
    monkeypatch.setenv("UNIFIED_BASE_URL", "https://proxy.example/v1")

    framework = CodexFramework()

    assert framework.get_network_endpoint_url() == "https://proxy.example/v1"


def test_codex_disables_server_side_product_tools(monkeypatch):
    monkeypatch.delenv("UNIFIED_API_KEY", raising=False)
    monkeypatch.delenv("UNIFIED_BASE_URL", raising=False)
    framework = CodexFramework(reasoning_effort="max")

    command = framework.build_run_command(
        model="gpt-5.6-sol",
        session_id="ignored",
        prompt_path="/tmp/prompt.txt",
    )
    init_script = framework.get_container_init_script("codex")

    for feature in framework.BENCHMARK_DISABLED_FEATURES:
        assert f"features.{feature}=false" in command
        assert repr(feature) in init_script
    assert "__CODEX_DISABLED_FEATURES__" not in init_script
    assert "*[f'{feature} = false'" in init_script
    assert 'web_search="disabled"' in command
    assert 'web_search = "disabled"' in init_script


def test_codex_uses_standalone_installer_without_npm():
    framework = CodexFramework(api_key="test-key", agent_version="0.145.0")

    init_script = framework.get_container_init_script("codex")

    assert "https://chatgpt.com/codex/install.sh" in init_script
    assert "'CODEX_RELEASE': target_version" in init_script
    assert "'CODEX_NON_INTERACTIVE': '1'" in init_script
    assert "'CODEX_HOME': '/opt/codex'" in init_script
    assert "'CODEX_INSTALL_DIR': '/usr/local/bin'" in init_script
    assert "npm', 'i'" not in init_script
    assert "deb.nodesource.com" not in init_script
