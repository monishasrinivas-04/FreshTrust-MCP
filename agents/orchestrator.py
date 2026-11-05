
import asyncio
import logging
import time
from typing import TypedDict

from langgraph.graph import StateGraph, END

from security.input_validation import parse_github_repo_url
from rag.cache import FileCache
from mcp_layer.github_client import GitHubMCPClient as GitHubRepositoryClient
from security.request_secret import encrypted_request_secret, get_request_secret
from rag.retriever import rank_repository
from rag.context_refiner import refine_context
from llm.cloudflare_client import generate_answer
from llm.prompts import classify_task

log = logging.getLogger(__name__)


class FreshTrustState(TypedDict, total=False):
    repo_url: str
    question: str
    visibility: str
    top_k: int
    owner: str
    repo: str
    files: list
    ranked: list
    context: str
    answer: str
    provenance: list
    freshness: list
    latency: dict
    retrieval_mode: str
    model: str
    task_type: str


def validate_node(state):
    owner, repo = parse_github_repo_url(state["repo_url"])
    visibility = state.get("visibility", "Public")

    if visibility not in {"Public", "Private"}:
        raise ValueError("Repository visibility must be Public or Private.")

    if visibility == "Private" and not get_request_secret():
        raise PermissionError(
            "A GitHub Personal Access Token is required for a private repository."
        )

    return {
        "owner": owner,
        "repo": repo,
        "task_type": classify_task(state["question"]),
    }


async def retrieve_node(state):
    start = time.perf_counter()
    visibility = state.get("visibility", "Public")
    token = get_request_secret() if visibility == "Private" else None

    cache = FileCache()
    repo_key = f"{state['owner']}/{state['repo']}"

    try:
        client = GitHubRepositoryClient()

        files = await client.retrieve(
            owner=state["owner"],
            repo=state["repo"],
            question=state["question"],
            max_files=30,
            visibility=visibility,
            token=token,
        )

        mode = "github_mcp"

        # Only public repository content may enter the shared persistent cache.
        if visibility == "Public":
            for item in files:
                cache.put(
                    repo_key,
                    item["path"],
                    item["content"],
                    revision=item.get("commit_sha"),
                    commit_sha=item.get("commit_sha"),
                    commit_timestamp=item.get("commit_timestamp"),
                )

    except Exception as exc:
        # Private requests must never fall back to shared persistent files.
        if visibility != "Public":
            raise

        message = str(exc).upper()

        if (
            "RATE_LIMIT" not in message
            and "RATE LIMIT" not in message
            and "429" not in message
        ):
            raise

        cached_files = []

        for cached_item in cache.list_repo(repo_key):
            fresh_item = cache.get(
                repo_key,
                cached_item["path"],
                revision=cached_item.get("revision"),
            )

            if fresh_item is not None:
                cached_files.append({
                    "path": cached_item["path"],
                    "content": fresh_item["content"],
                    "commit_sha": fresh_item.get("commit_sha"),
                    "commit_timestamp": fresh_item.get("commit_timestamp"),
                })

        if not cached_files:
            raise RuntimeError(
                "GitHub rate limit reached and no unexpired cached repository "
                "content is available. Please retry after the limit resets."
            ) from exc

        files = cached_files
        mode = "validated_cache_fallback"

    if not files:
        raise RuntimeError(
            "No readable repository files were retrieved. Check repository URL, "
            "visibility, and access permissions."
        )

    return {
        "files": files,
        "latency": {
            **state.get("latency", {}),
            "repository_retrieval_seconds": time.perf_counter() - start,
        },
        "retrieval_mode": mode,
    }


def rank_node(state):
    start = time.perf_counter()

    visibility = state.get("visibility", "Public")

    # Keep the request-scoped token available for any future metadata lookup.
    token = get_request_secret() if visibility == "Private" else None

    # Persistent embedding cache is permitted only for public repositories.
    cache_embeddings = visibility == "Public"

    ranked, metrics = rank_repository(
        state["question"],
        state["files"],
        state["owner"],
        state["repo"],
        token,
        state.get("top_k", 5),
        cache_embeddings=cache_embeddings,
    )

    context_start = time.perf_counter()
    context = refine_context(ranked)
    context_seconds = time.perf_counter() - context_start

    latency = {
        **state.get("latency", {}),
        **metrics,
        "context_refinement_seconds": context_seconds,
        # This wall-time measurement includes embedding/ranking and context refinement.
        "retrieval_ranking_seconds": time.perf_counter() - start,
    }

    return {
        "ranked": ranked,
        "context": context,
        "latency": latency,
    }


def generate_node(state):
    task_type = state.get("task_type") or classify_task(state["question"])

    answer, elapsed = generate_answer(
        state["question"],
        state["context"],
        task_type=task_type,
    )

    from config.settings import settings

    return {
        "answer": answer,
        "task_type": task_type,
        "model": settings.cloudflare_model,
        "latency": {
            **state.get("latency", {}),
            "generation_seconds": elapsed,
        },
    }


def format_node(state):
    ranked = state["ranked"]

    provenance = [
        [
            item["path"],
            f"{item['start_line']}-{item['end_line']}",
            round(
                float(item.get("rrf_score", item.get("relevance_score", 0.0))),
                6,
            ),
            item.get("commit_sha") or "unknown",
            f"https://github.com/{state['owner']}/{state['repo']}/blob/HEAD/{item['path']}",
        ]
        for item in ranked
    ]

    freshness = [
        [
            item["path"],
            item.get("commit_timestamp"),
            item.get("freshness_score"),
            item.get("freshness_status") or "unknown",
            item.get("commit_sha"),
        ]
        for item in ranked
    ]

    return {
        "provenance": provenance,
        "freshness": freshness,
    }


def build_graph():
    graph = StateGraph(FreshTrustState)

    graph.add_node("validate", validate_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("rank", rank_node)
    graph.add_node("generate", generate_node)
    graph.add_node("format", format_node)

    graph.set_entry_point("validate")
    graph.add_edge("validate", "retrieve")
    graph.add_edge("retrieve", "rank")
    graph.add_edge("rank", "generate")
    graph.add_edge("generate", "format")
    graph.add_edge("format", END)

    return graph.compile()


_GRAPH = build_graph()


def run_query(
    repo_url,
    question,
    visibility="Public",
    github_token="",
    top_k=5,
):
    pipeline_start = time.perf_counter()

    state = {
        "repo_url": repo_url,
        "question": question,
        "visibility": visibility,
        "top_k": top_k,
        "latency": {},
    }

    with encrypted_request_secret(
        github_token if visibility == "Private" else ""
    ):
        result = asyncio.run(_GRAPH.ainvoke(state))

    result["latency"]["total_seconds"] = time.perf_counter() - pipeline_start

    result["latency"]["latency_accounting"] = (
        "total_seconds is end-to-end wall time; substage metrics may overlap."
    )

    return result
