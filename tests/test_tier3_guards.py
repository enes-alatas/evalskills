import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import tier3  # noqa: E402

FAKE_TOKEN = "gw-test-0000-not-a-real-token"


@pytest.fixture
def safe(tmp_path):
    tok = tmp_path / "token"
    tok.write_text(FAKE_TOKEN + "\n")
    return {
        "DOCKER_HOST": tier3.EVALS_SOCKET,
        "TIER3_GATEWAY_URL": "http://10.0.0.1:4000/v1",
        "TIER3_GATEWAY_TOKEN_FILE": str(tok),
    }


def test_a_safe_setup_gets_only_the_gateway_token(safe):
    env = tier3.trial_env(safe)
    assert env["DOCKER_HOST"] == tier3.EVALS_SOCKET
    assert env["SKILL_EVAL_LLM_PROVIDER"] == "openai-compatible"
    assert env["SKILL_EVAL_LLM_API_KEY"] == FAKE_TOKEN
    assert not any(k in env for k in ("ANTHROPIC_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY"))


@pytest.mark.parametrize("mode", ["local", "e2b", "modal", "daytona", "kata"])
def test_refuses_any_env_mode_but_docker(safe, monkeypatch, mode):
    monkeypatch.setattr(tier3, "ENV_MODE", mode)
    with pytest.raises(tier3.Unsafe, match="env-mode"):
        tier3.trial_env(safe)


@pytest.mark.parametrize("host", [None, "", "unix:///var/run/docker.sock", "tcp://10.0.0.2:2375"])
def test_refuses_any_daemon_but_docker_evals(safe, host):
    safe.pop("DOCKER_HOST")
    if host is not None:
        safe["DOCKER_HOST"] = host
    with pytest.raises(tier3.Unsafe, match="DOCKER_HOST"):
        tier3.trial_env(safe)


@pytest.mark.parametrize("key", tier3.REAL_KEYS)
def test_refuses_when_a_real_provider_key_is_present(safe, key):
    safe[key] = "sk-test-0000-not-a-real-key"
    with pytest.raises(tier3.Unsafe, match=key):
        tier3.trial_env(safe)


@pytest.mark.parametrize("missing", ["TIER3_GATEWAY_URL", "TIER3_GATEWAY_TOKEN_FILE"])
def test_refuses_without_the_gateway(safe, missing):
    safe.pop(missing)
    with pytest.raises(tier3.Unsafe, match="gateway"):
        tier3.trial_env(safe)


def test_refuses_an_unreadable_or_empty_token(safe, tmp_path):
    safe["TIER3_GATEWAY_TOKEN_FILE"] = str(tmp_path / "absent")
    with pytest.raises(tier3.Unsafe, match="cannot read"):
        tier3.trial_env(safe)
    (tmp_path / "empty").write_text("\n")
    safe["TIER3_GATEWAY_TOKEN_FILE"] = str(tmp_path / "empty")
    with pytest.raises(tier3.Unsafe, match="empty"):
        tier3.trial_env(safe)
