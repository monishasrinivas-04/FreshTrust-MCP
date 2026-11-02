import asyncio
import logging
import time
from typing import TypedDict

from langgraph.graph import StateGraph, END

from security.input_validation import parse_github_repo_url
from security.credential_manager import request_credential
from security.authentication import validate_github_access
from rag.cache import FileCache
from mcp_layer.github_client import GitHubMCPClient
from rag.retriever import rank_repository
from rag.context_refiner import refine_context
from llm.cloudflare_client import generate_answer

log = logging.getLogger(__name__)


class FreshTrustState(TypedDict, total=False):
    repo_url: str
    question: str
    visibility: str
    github_token: str
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


def validate_node(state):
    owner, repo = parse_github_repo_url(state["repo_url"])
    visibility = state.get("visibility", "Public")

    if visibility not in {"Public", "Private"}:
        raise ValueError("Repository visibility must be Public or Private.")

    if visibility == "Private" and not state.get("github_token"):
        raise PermissionError(
            "A GitHub Personal Access Token is required for a private repository."
        )

    return {"owner": owner, "repo": repo}


async def retrieve_node(state):
    start = time.perf_counter()
    visibility = state.get("visibility", "Public")
    supplied_token = state.get("github_token", "")

    with request_credential(supplied_token) as token:
        validate_github_access(
            state["owner"],
            state["repo"],
            token,
            private=(visibility == "Private"),
        )

        cache = FileCache()
        repo_key = f"{state['owner']}/{state['repo']}"

        try:
            client = GitHubMCPClient(token if visibility == "Private" else None)
            files = await client.retrieve(
                owner=state["owner"],
                repo=state["repo"],
                question=state["question"],
                max_files=6,
                visibility=visibility,
            )

            for item in files:
                cache.put(repo_key, item["path"], item["content"])

            mode = "public_rest" if visibility == "Public" else "private_mcp"

        except Exception as exc:
            if "RATE_LIMIT" not in str(exc).upper() and "429" not in str(exc):
                raise

            cached_files = []
            for item in cache.list_repo(repo_key):
                fresh_item = cache.get(repo_key, item["path"])
                if fresh_item is not None:
                    cached_files.append({
                        "path": item["path"],
                        "content": fresh_item["content"],
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

    ranked, metrics = rank_repository(
        state["question"],
        state["files"],
        state["owner"],
        state["repo"],
        state.get("github_token"),
        state.get("top_k", 5),
    )

    context_start = time.perf_counter()
    context = refine_context(ranked)
    context_seconds = time.perf_counter() - context_start

    latency = {
        **state.get("latency", {}),
        **metrics,
        "context_refinement_seconds": context_seconds,
        # This is the complete rank node wall time. It overlaps with the
        # embedding_and_ranking substage and must not be summed into total.
        "retrieval_ranking_seconds": time.perf_counter() - start,
    }

    return {"ranked": ranked, "context": context, "latency": latency}


def generate_node(state):
    answer, elapsed = generate_answer(state["question"], state["context"])

    from config.settings import settings

    return {
        "answer": answer,
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
            round(item.get("semantic_score", 0), 4),
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
            item.get("freshness_status"),
            item.get("commit_sha"),
        ]
        for item in ranked
    ]

    return {"provenance": provenance, "freshness": freshness}


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
        "github_token": github_token,
        "top_k": top_k,
        "latency": {},
    }

    result = asyncio.run(_GRAPH.ainvoke(state))

    # True wall-clock duration; do not sum overlapping substage measurements.
    result["latency"]["total_seconds"] = time.perf_counter() - pipeline_start
    result["latency"]["latency_accounting"] = (
        "total_seconds is end-to-end wall time; substage metrics may overlap."
    )

    result.pop("github_token", None)
    return result
