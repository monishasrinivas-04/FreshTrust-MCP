
import pytest

from security.authorization import authorize_tool


OWNER = "monishasrinivas-04"
REPO = "FreshTrust-MCP"


def test_file_read_for_requested_repository():
    assert authorize_tool(
        "get_file_contents",
        {
            "owner": OWNER,
            "repo": REPO,
            "path": "README.md",
        },
        OWNER,
        REPO,
    )


def test_search_for_requested_repository():
    assert authorize_tool(
        "search_code",
        {
            "query": (
                f"repo:{OWNER}/{REPO} "
                "authentication"
            )
        },
        OWNER,
        REPO,
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "create_branch",
        "create_or_update_file",
        "delete_file",
        "push_files",
        "merge_pull_request",
        "issue_write",
    ],
)
def test_write_tools_are_denied(tool_name):
    with pytest.raises(PermissionError):
        authorize_tool(
            tool_name,
            {
                "owner": OWNER,
                "repo": REPO,
            },
            OWNER,
            REPO,
        )


def test_file_read_cannot_change_repository():
    with pytest.raises(PermissionError):
        authorize_tool(
            "get_file_contents",
            {
                "owner": "another-user",
                "repo": "another-repo",
                "path": "README.md",
            },
            OWNER,
            REPO,
        )


def test_search_cannot_target_another_repository():
    with pytest.raises(PermissionError):
        authorize_tool(
            "search_code",
            {
                "query": (
                    "repo:another-user/another-repo "
                    "authentication"
                )
            },
            OWNER,
            REPO,
        )


def test_search_cannot_add_another_repository_scope():
    with pytest.raises(PermissionError):
        authorize_tool(
            "search_code",
            {
                "query": (
                    f"repo:{OWNER}/{REPO} "
                    "repo:another-user/another-repo"
                )
            },
            OWNER,
            REPO,
        )


def test_file_path_traversal_is_denied():
    with pytest.raises(PermissionError):
        authorize_tool(
            "get_file_contents",
            {
                "owner": OWNER,
                "repo": REPO,
                "path": "../another-repo/README.md",
            },
            OWNER,
            REPO,
        )
