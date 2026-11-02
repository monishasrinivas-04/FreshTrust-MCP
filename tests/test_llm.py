from types import SimpleNamespace
from llm import cloudflare_client
from llm.cloudflare_client import generate_answer, InferenceUnavailable

def test_missing_credentials_fails_closed(monkeypatch):
    monkeypatch.setattr(cloudflare_client, "settings",
                        SimpleNamespace(cloudflare_account_id="", cloudflare_api_token="",
                                        cloudflare_model="@cf/qwen/qwen3.8-27b"))
    try:
        generate_answer("q", "context")
    except InferenceUnavailable:
        return
    assert False
