import pytest
from security.input_validation import parse_github_repo_url,validate_repo_path
from security.authorization import authorize_tool
def test_parse_repo():
    assert parse_github_repo_url("https://github.com/openai/openai-python") == ("openai","openai-python")
def test_reject_non_github():
    with pytest.raises(ValueError): parse_github_repo_url("http://evil.test/a/b")
def test_path_traversal():
    with pytest.raises(ValueError): validate_repo_path("../secret")
def test_tool_allowlist():
    with pytest.raises(PermissionError): authorize_tool("delete_repo",{}, "a","b")
