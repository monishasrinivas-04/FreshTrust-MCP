
"""FreshTrust-MCP application. Public repositories are anonymous; private repositories use a request-scoped PAT."""

from __future__ import annotations

import logging
import os
import time
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv
from fastapi import FastAPI
import uvicorn

from agents.orchestrator import run_query
from evaluation.metrics import (
    calculate_run_metrics,
    record_execution,
    record_human_review,
    dashboard_summary,
)
from mcp_layer.github_client import GitHubMCPClient, close_public_mcp
from security.redaction import redact


# --------------------------------------------------
# 1. CONFIGURATION
# --------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=False)

DEFAULT_TOP_K = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("FreshTrust-MCP")


# --------------------------------------------------
# 2. ERROR DIAGNOSTICS
# --------------------------------------------------

def describe_error(error, depth=0):
    indent = "  " * depth

    try:
        message = redact(str(error))[:500]
    except Exception:
        message = "Error details unavailable."

    details = [f"{indent}{type(error).__name__}: {message}"]

    nested_errors = getattr(error, "exceptions", None)

    if nested_errors:
        for child in nested_errors:
            details.extend(describe_error(child, depth + 1))
    elif error.__cause__ is not None:
        details.extend(describe_error(error.__cause__, depth + 1))

    return details


# --------------------------------------------------
# 4. REPOSITORY VISIBILITY
# --------------------------------------------------

def toggle_repository_mode(visibility):
    if visibility == "Private":
        return (
            gr.update(visible=False, value=""),
            gr.update(visible=True, value=None, choices=[]),
            gr.update(visible=True, value=""),
            gr.update(visible=True),
            "Private mode: enter your PAT, then discover and select a repository. "
            "The PAT is used for the current request and is not saved.",
        )

    return (
        gr.update(visible=True, value=""),
        gr.update(visible=False, value=None, choices=[]),
        gr.update(visible=False, value=""),
        gr.update(visible=False),
        "Public mode: enter the repository URL. Access is anonymous; no login, OAuth, PAT, or admin key is required.",
    )


def discover_private_repositories(github_token):
    token_value = (github_token or "").strip()
    if not token_value:
        return (
            gr.update(choices=[], value=None),
            "Enter your GitHub Personal Access Token before discovering repositories.",
        )

    try:
        repositories = GitHubMCPClient.discover_repositories(token_value)
        choices = [item["full_name"] for item in repositories]

        if not choices:
            return (
                gr.update(choices=[], value=None),
                "No private repositories were returned for this token. "
                "Check its repository permissions and organization access.",
            )

        return (
            gr.update(choices=choices, value=None),
            f"Found **{len(choices)}** accessible private repositories. Select one below.",
        )
    except Exception as exc:
        # Never log the PAT or raw subprocess environment.
        logger.warning(
            "Private repository discovery failed (%s).",
            type(exc).__name__,
        )
        return (
            gr.update(choices=[], value=None),
            "Repository discovery failed. Verify the PAT permissions and Docker "
            "GitHub MCP Server connection, then try again.",
        )


# --------------------------------------------------
# 5. EVALUATION DISPLAY HELPERS
# --------------------------------------------------

def _metric_rows(metrics):
    rows = []

    sections = [
        ("Retrieval", metrics.get("retrieval", {})),
        ("Freshness", metrics.get("freshness", {})),
        ("Response grounding", metrics.get("response", {})),
        ("Latency", metrics.get("latency", {})),
    ]

    for section, values in sections:
        for key, value in values.items():
            display = round(value, 4) if isinstance(value, float) else value
            rows.append([section, key.replace("_", " ").title(), display])

    return rows


def _metric_summary_markdown(metrics):
    """Compact, always-visible summary of the current run."""
    retrieval = metrics.get("retrieval", {})
    freshness = metrics.get("freshness", {})
    response = metrics.get("response", {})
    latency_data = metrics.get("latency", {})

    def fmt(value, suffix=""):
        return "Not measured" if value is None else f"{value}{suffix}"

    labelled = retrieval.get("labelled_evaluation", False)
    note = (
        "Expected relevant-file labels were supplied for this run."
        if labelled else
        "Precision@5, Recall@5 and MRR require expected relevant file paths; supply them above to calculate these metrics."
    )
    return (
        "### Run metrics\\n"
        f"- **Precision@5:** {fmt(retrieval.get('precision_at_5'))}  |  "
        f"**Recall@5:** {fmt(retrieval.get('recall_at_5'))}  |  "
        f"**MRR:** {fmt(retrieval.get('mrr'))}\\n"
        f"- **Mean source freshness:** {fmt(freshness.get('mean_freshness_score'))}  |  "
        f"**Freshness coverage:** {fmt(freshness.get('freshness_coverage_pct'), '%')}\\n"
        f"- **Citation provenance precision:** {fmt(response.get('citation_provenance_precision_pct'), '%')}  |  "
        f"**End-to-end latency:** {fmt(latency_data.get('total_seconds'), ' s')}\\n\\n"
        f"*{note} Freshness measures source commit recency, not retrieval accuracy.*"
    )


def _history_rows():
    summary = dashboard_summary()

    return [
        ["Runs recorded", summary["runs_recorded"]],
        ["Success rate (%)", summary["success_rate_pct"]],
        ["Failures", summary["failure_count"]],
        ["Median end-to-end (s)", summary["median_end_to_end_seconds"]],
        ["P95 end-to-end (s)", summary["p95_end_to_end_seconds"]],
        ["Human reviews", summary["human_reviews"]],
        ["Mean human quality rating (1-5)", summary["mean_human_quality_rating"]],
        ["Human-verified grounded (%)", summary["human_grounded_pct"]],
        ["Human-verified complete (%)", summary["human_complete_pct"]],
    ]


# --------------------------------------------------
# 6. REPOSITORY ANALYSIS
# --------------------------------------------------

def ask(
    public_repo_url,
    visibility,
    github_token,
    private_repository,
    question,
    expected_relevant_files,
):
    run_started = time.perf_counter()

    if visibility == "Private":
        selected = (private_repository or "").strip()
        if not selected or "/" not in selected:
            return (
                "Enter your PAT, discover repositories, and select a private repository.",
                [], [], [], [], "Metrics will appear after a successful analysis.", _history_rows(), "Repository selection required",
            )
        repo_key = f"https://github.com/{selected}"
    else:
        repo_key = (public_repo_url or "").strip()
        if not repo_key:
            return (
                "Please enter a public GitHub repository URL.",
                [], [], [], [], "Metrics will appear after a successful analysis.", _history_rows(), "Not run",
            )

    if not question or not question.strip():
        return (
            "Please enter a question about the repository.",
            [], [], [], [], "Metrics will appear after a successful analysis.", _history_rows(), "Not run",
        )

    if visibility == "Private" and not (github_token and github_token.strip()):
        return (
            "A GitHub Personal Access Token is required for private repositories.",
            [], [], [], [], "Metrics will appear after a successful analysis.", _history_rows(), "Authentication required",
        )

    try:
        logger.info(
            "Starting repository analysis. Visibility: %s",
            visibility,
        )

        token_to_use = (
            github_token.strip()
            if visibility == "Private" and github_token
            else ""
        )

        result = run_query(
            repo_url=repo_key,
            question=question.strip(),
            visibility=visibility,
            github_token=token_to_use,
            top_k=DEFAULT_TOP_K,
        )

        answer_text = result.get("answer", "")

        if not answer_text or not answer_text.strip():
            raise RuntimeError(
                "The analysis pipeline completed without generating an answer."
            )

        provenance = result.get("provenance", [])
        freshness = result.get("freshness", [])
        latency_data = result.get("latency", {})

        elapsed = time.perf_counter() - run_started
        latency_data["ui_end_to_end_seconds"] = elapsed

        metrics = calculate_run_metrics(
            answer=answer_text,
            provenance=provenance,
            freshness=freshness,
            latency=latency_data,
            expected_relevant_paths=expected_relevant_files,
            k=DEFAULT_TOP_K,
        )

        record_execution(
            repo_key=repo_key,
            success=True,
            elapsed_seconds=elapsed,
            metrics=metrics,
            retrieval_mode=result.get("retrieval_mode", "unknown"),
        )

        latency_rows = [
            [key.replace("_", " ").title(), round(value, 4)]
            for key, value in latency_data.items()
            if isinstance(value, (int, float))
        ]

        # Convert commit timestamps to age in hours for display.
        # This is source recency, not retrieval accuracy.
        freshness_display = []
        now = datetime.now(timezone.utc)

        for row in freshness:
            if not row:
                continue

            timestamp = row[1] if len(row) > 1 else None
            age_hours = None

            if timestamp:
                try:
                    parsed = datetime.fromisoformat(
                        str(timestamp).replace("Z", "+00:00")
                    )

                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)

                    age_hours = round(
                        max(
                            0.0,
                            (
                                now - parsed.astimezone(timezone.utc)
                            ).total_seconds() / 3600,
                        ),
                        2,
                    )
                except (TypeError, ValueError):
                    age_hours = None

            freshness_display.append([
                row[0],
                age_hours,
                row[2] if len(row) > 2 else None,
                row[3] if len(row) > 3 else "unknown",
                row[4] if len(row) > 4 else None,
            ])

        status = (
            f"Success Â· {result.get('retrieval_mode', 'unknown')} retrieval Â· "
            f"{result.get('model', 'Qwen')}"
        )

        logger.info("Repository analysis completed successfully.")

        return (
            answer_text,
            provenance,
            freshness_display,
            latency_rows,
            _metric_rows(metrics),
            _metric_summary_markdown(metrics),
            _history_rows(),
            status,
        )

    except Exception as exc:
        elapsed = time.perf_counter() - run_started

        record_execution(
            repo_key=repo_key or "invalid-repository",
            success=False,
            elapsed_seconds=elapsed,
            error_type=type(exc).__name__,
        )

        error_details = "\n".join(describe_error(exc))
        logger.error("Repository analysis failed:\n%s", error_details)

        error_message = (
            "### Repository analysis failed\n\n"
            "```text\n"
            + f"{error_details[:2000]}\n"
            + "```\n\n"
            "Check the application terminal for diagnostic details."
        )

        return (
            error_message,
            [], [], [], [], "Metrics unavailable because this analysis failed.", _history_rows(), "Failed",
        )


# --------------------------------------------------
# 7. HUMAN EVALUATION
# --------------------------------------------------

def save_review(
    public_repo_url,
    visibility,
    private_repository,
    quality_rating,
    grounded,
    complete,
):
    if visibility == "Private":
        selected = (private_repository or "").strip()
        repo_key = f"https://github.com/{selected}" if selected else ""
    else:
        repo_key = (public_repo_url or "").strip()

    if not repo_key:
        return "Select or enter a repository before saving a review.", _history_rows()

    try:
        record_human_review(
            repo_key=repo_key,
            quality_rating=int(quality_rating),
            grounded=bool(grounded),
            complete=bool(complete),
        )
        return "Human review saved successfully.", _history_rows()
    except Exception as exc:
        logger.error(
            "Could not save human evaluation review (%s).",
            type(exc).__name__,
        )
        return "Could not save the human review. Check the application logs.", _history_rows()


# --------------------------------------------------
# 8. GRADIO INTERFACE
# --------------------------------------------------

CSS = """
.gradio-container {
    max-width: 1180px !important;
    margin: auto !important;
}

#hero {
    padding: 22px 26px;
    border: 1px solid var(--border-color-primary);
    border-radius: 16px;
}
"""


with gr.Blocks(
    title="FreshTrust-MCP",
    theme=gr.themes.Soft(),
    css=CSS,
) as demo:

    gr.HTML(
        """
        <div id="hero">
            <h1>FreshTrust-MCP</h1>
            <p>
                Secure, adaptive and freshness-aware repository intelligence
            </p>
            <small>
                GitHub MCP retrieval Â· Semantic RAG Â· Source freshness Â·
                Evidence-grounded answers
            </small>
        </div>
        """
    )

    gr.Markdown("## Repository analysis")

    visibility = gr.Radio(
        choices=["Public", "Private"],
        value="Public",
        label="Repository visibility",
    )

    repo = gr.Textbox(
        label="Public GitHub repository URL",
        placeholder="https://github.com/owner/repository",
        visible=True,
    )

    token = gr.Textbox(
        label="GitHub Personal Access Token",
        type="password",
        placeholder="Paste your PAT to discover repositories you can access",
        visible=False,
    )

    with gr.Row():
        discover_button = gr.Button(
            "Discover private repositories",
            visible=False,
        )
        private_repo = gr.Dropdown(
            label="Select a private repository",
            choices=[],
            value=None,
            interactive=True,
            visible=False,
        )

    token_info = gr.Markdown(
        "Public mode: enter the repository URL. Access is anonymous; no login, OAuth, PAT, or admin key is required."
    )
    discovery_status = gr.Markdown()

    question = gr.Textbox(
        label="Ask about the repository",
        lines=3,
        placeholder=(
            "Summarize this repository, explain its architecture, "
            "or ask about its implementation."
        ),
    )

    expected_relevant_files = gr.Textbox(
        label="Expected relevant files (optional; for labelled retrieval evaluation)",
        placeholder="Example: README.md, app.py, requirements.txt",
        info=(
            "Enter comma-separated files known to contain the answer. "
            "Leave blank for unlabelled runs."
        ),
    )

    gr.Markdown(
        "*The system automatically retrieves the 5 most relevant evidence chunks.*"
    )

    with gr.Row():
        submit = gr.Button("Analyze repository", variant="primary")
        clear = gr.ClearButton([repo, token, private_repo, question])

    status = gr.Markdown("Ready")
    answer = gr.Markdown(label="Answer")
    metrics_summary = gr.Markdown("Run an analysis to display evaluation metrics, freshness, and latency.")

    with gr.Accordion("Source provenance", open=True):
        provenance = gr.Dataframe(
            headers=[
                "File",
                "Lines",
                "Similarity",
                "Commit SHA",
                "Source URL",
            ],
            interactive=False,
        )

    with gr.Accordion("Freshness details", open=True):
        freshness = gr.Dataframe(
            headers=[
                "File",
                "Age (hours)",
                "Freshness score",
                "Status",
                "Commit SHA",
            ],
            interactive=False,
        )

        gr.Markdown(
            "**Interpretation:** Age in hours and the freshness score describe "
            "source commit recency. They do not measure retrieval accuracy."
        )

    with gr.Accordion("Latency details", open=True):
        latency = gr.Dataframe(
            headers=["Stage", "Seconds"],
            interactive=False,
        )

    with gr.Accordion("Detailed evaluation metrics", open=True):
        evaluation = gr.Dataframe(
            headers=["Dimension", "Metric", "Measured value"],
            interactive=False,
        )

        gr.Markdown(
            "**Metric interpretation:** Precision@5, Recall@5 and MRR require "
            "expected relevant-file labels. Similarity is a ranking proxy, not "
            "retrieval accuracy. Citation provenance checks validate source/line "
            "references, but do not replace human answer-quality assessment."
        )

    with gr.Accordion("Historical reliability and latency", open=False):
        history = gr.Dataframe(
            headers=["Metric", "Value"],
            value=_history_rows(),
            interactive=False,
        )

    with gr.Accordion("Human response-quality review", open=False):
        quality_rating = gr.Slider(
            minimum=1,
            maximum=5,
            step=1,
            value=3,
            label="Answer quality rating (1 = poor, 5 = excellent)",
        )

        grounded = gr.Checkbox(
            label="The answer's factual claims are supported by its cited evidence",
            value=False,
        )

        complete = gr.Checkbox(
            label="The answer sufficiently addresses the question",
            value=False,
        )

        save_review_button = gr.Button("Save human evaluation")
        review_status = gr.Markdown()

        save_review_button.click(
            fn=save_review,
            inputs=[
                repo,
                visibility,
                private_repo,
                quality_rating,
                grounded,
                complete,
            ],
            outputs=[review_status, history],
        )

    inputs = [
        repo,
        visibility,
        token,
        private_repo,
        question,
        expected_relevant_files,
    ]

    outputs = [
        answer,
        provenance,
        freshness,
        latency,
        evaluation,
        metrics_summary,
        history,
        status,
    ]

    visibility.change(
        fn=toggle_repository_mode,
        inputs=[visibility],
        outputs=[repo, private_repo, token, discover_button, token_info],
    )

    discover_button.click(
        fn=discover_private_repositories,
        inputs=[token],
        outputs=[private_repo, discovery_status],
    )

    submit.click(
        fn=ask,
        inputs=inputs,
        outputs=outputs,
    )

    question.submit(
        fn=ask,
        inputs=inputs,
        outputs=outputs,
    )


# Host Gradio through FastAPI. No administrator OAuth callback is required.
app = FastAPI(title="FreshTrust-MCP API")
app = gr.mount_gradio_app(app, demo, path="/")

@app.on_event("startup")
async def _open_local_browser():
    # Avoid opening a browser on hosted/headless deployments.
    if os.getenv("RENDER") or os.getenv("DYNO") or os.getenv("K_SERVICE"):
        return
    port = int(os.environ.get("PORT", "7860"))
    local_url = f"http://127.0.0.1:{port}/"
    logger.info("Opening FreshTrust-MCP UI at %s", local_url)
    threading.Timer(1.2, lambda: webbrowser.open(local_url, new=2)).start()


@app.on_event("shutdown")
async def _shutdown_public_github_mcp():
    close_public_mcp()


# --------------------------------------------------
# 10. APPLICATION ENTRY POINT
# --------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting FreshTrust-MCP with FastAPI and Gradio.")

    port = int(os.environ.get("PORT", "7860"))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
