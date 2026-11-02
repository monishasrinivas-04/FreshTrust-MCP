
import logging
import time
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

from agents.orchestrator import run_query
from security.redaction import redact
from evaluation.metrics import (calculate_run_metrics, record_execution, record_human_review, dashboard_summary)


# --------------------------------------------------
# 1. CONFIGURATION
# --------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=False)

# Fixed retrieval depth. Not exposed to users.
DEFAULT_TOP_K = 5


# --------------------------------------------------
# 2. LOGGING
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("FreshTrust-MCP")


# --------------------------------------------------
# 3. CUSTOM CSS
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


# --------------------------------------------------
# 4. ERROR DIAGNOSTICS
# --------------------------------------------------

def describe_error(error, depth=0):

    indent = "  " * depth

    try:
        message = redact(str(error))[:500]
    except Exception:
        message = "Error details unavailable."

    details = [
        f"{indent}{type(error).__name__}: {message}"
    ]

    # Extract nested ExceptionGroup errors.
    nested_errors = getattr(error, "exceptions", None)

    if nested_errors:
        for child in nested_errors:
            details.extend(
                describe_error(child, depth + 1)
            )

    elif error.__cause__ is not None:
        details.extend(
            describe_error(error.__cause__, depth + 1)
        )

    return details


# --------------------------------------------------
# 5. TOKEN VISIBILITY CONTROL
# --------------------------------------------------

def toggle_token_field(visibility):

    if visibility == "Private":
        return (
            gr.update(
                visible=True,
                value=""
            ),
            "A GitHub Personal Access Token is required for private repositories."
        )

    return (
        gr.update(
            visible=False,
            value=""
        ),
        "Public repositories can be accessed using their URL alone."
    )


# --------------------------------------------------
# 6. REPOSITORY ANALYSIS
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
            if isinstance(value, float):
                display = round(value, 4)
            else:
                display = value
            rows.append([section, key.replace("_", " ").title(), display])
    return rows


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


def ask(repo_url, visibility, github_token, question, expected_relevant_files):
    run_started = time.perf_counter()
    repo_key = (repo_url or "").strip()

    if not repo_key:
        return (
            "Please enter a GitHub repository URL.", [], [], [], [],
            _history_rows(), "Not run"
        )

    if not question or not question.strip():
        return (
            "Please enter a question about the repository.", [], [], [], [],
            _history_rows(), "Not run"
        )

    if visibility == "Private" and not (github_token and github_token.strip()):
        return (
            "A GitHub Personal Access Token is required for private repositories.",
            [], [], [], [], _history_rows(), "Authentication required"
        )

    try:
        logger.info("Starting repository analysis. Visibility: %s", visibility)
        token_to_use = (
            github_token.strip()
            if visibility == "Private" and github_token else ""
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

        # Use the actual outer wall-clock duration as the authoritative run latency.
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

        # Freshness output is timestamp-based in the backend. Convert to age
        # in hours for the UI without changing the stored commit timestamp.
        freshness_display = []
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for row in freshness:
            if not row:
                continue
            timestamp = row[1] if len(row) > 1 else None
            age_hours = None
            if timestamp:
                try:
                    parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    age_hours = round(max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds() / 3600), 2)
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
            f"Success · {result.get('retrieval_mode', 'unknown')} retrieval · "
            f"{result.get('model', 'Qwen')}"
        )
        logger.info("Repository analysis completed successfully.")

        return (
            answer_text,
            provenance,
            freshness_display,
            latency_rows,
            _metric_rows(metrics),
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
            "```text\n" + f"{error_details[:2000]}\n" + "```\n\n"
            "Check the application terminal for diagnostic details."
        )
        return (
            error_message, [], [], [], [], _history_rows(), "Failed"
        )


def save_review(repo_url, quality_rating, grounded, complete):
    if not repo_url or not repo_url.strip():
        return "Enter the repository URL before saving a review.", _history_rows()
    try:
        record_human_review(
            repo_key=repo_url.strip(),
            quality_rating=int(quality_rating),
            grounded=bool(grounded),
            complete=bool(complete),
        )
        return "Human review saved to the local evaluation log.", _history_rows()
    except Exception as exc:
        logger.exception("Could not save human evaluation review.")
        return f"Could not save review: {type(exc).__name__}", _history_rows()


# --------------------------------------------------
# 7. GRADIO INTERFACE
# --------------------------------------------------

with gr.Blocks(
    title="FreshTrust-MCP",
    theme=gr.themes.Soft(),
    css=CSS
) as demo:

    # Application header.
    gr.HTML(
        """
        <div id="hero">
            <h1>FreshTrust-MCP</h1>
            <p>
                Secure, adaptive and freshness-aware repository intelligence
            </p>
            <small>
                GitHub MCP · Semantic RAG · Source freshness ·
                Evidence-grounded answers
            </small>
        </div>
        """
    )

    # Repository configuration.
    with gr.Row():

        repo = gr.Textbox(
            label="GitHub repository URL",
            placeholder="https://github.com/owner/repository",
            scale=3
        )

        visibility = gr.Radio(
            choices=["Public", "Private"],
            value="Public",
            label="Visibility",
            scale=1
        )

    # Token field: hidden by default for public repositories.
    token = gr.Textbox(
        label="GitHub Personal Access Token",
        type="password",
        placeholder="Enter your token for private repository access",
        visible=False
    )

    token_info = gr.Markdown(
        "Public repositories can be accessed using their URL alone."
    )

    # Repository question.
    question = gr.Textbox(
        label="Ask about the repository",
        lines=3,
        placeholder=(
            "Summarize this repository, explain its architecture, "
            "or ask about its implementation."
        )
    )

    expected_relevant_files = gr.Textbox(
        label="Expected relevant files (optional; for labelled retrieval evaluation)",
        placeholder="Example: README.md, app.py, requirements.txt",
        info="Enter comma-separated files known to contain the answer. Leave blank for unlabelled runs."
    )

    # Fixed retrieval configuration.
    gr.Markdown(
        "*The system automatically retrieves the 5 most relevant evidence chunks.*"
    )

    # Action buttons.
    with gr.Row():

        submit = gr.Button(
            "Analyze repository",
            variant="primary"
        )

        clear = gr.ClearButton(
            [repo, token, question]
        )

    # Execution status.
    status = gr.Markdown("Ready")

    # Generated answer.
    answer = gr.Markdown(
        label="Answer"
    )

    # Source provenance.
    with gr.Accordion(
        "Source provenance",
        open=True
    ):

        provenance = gr.Dataframe(
            headers=[
                "File",
                "Lines",
                "Similarity",
                "Commit SHA",
                "Source URL"
            ],
            interactive=False
        )

    # Freshness information.
    with gr.Accordion(
        "Freshness",
        open=False
    ):

        freshness = gr.Dataframe(
            headers=[
                "File",
                "Age (hours)",
                "Freshness score",
                "Status",
                "Commit SHA"
            ],
            interactive=False
        )

    # Performance information.
    with gr.Accordion(
        "Performance",
        open=False
    ):

        latency = gr.Dataframe(
            headers=[
                "Stage",
                "Seconds"
            ],
            interactive=False
        )

    # Evaluation metrics: labelled retrieval metrics are shown only when
    # expected relevant files are supplied. Similarity and citation checks
    # are explicitly labelled as proxies.
    with gr.Accordion("Evaluation metrics", open=True):
        evaluation = gr.Dataframe(
            headers=["Dimension", "Metric", "Measured value"],
            interactive=False
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
            interactive=False
        )

    with gr.Accordion("Human response-quality review", open=False):
        quality_rating = gr.Slider(
            minimum=1, maximum=5, step=1, value=3,
            label="Answer quality rating (1 = poor, 5 = excellent)"
        )
        grounded = gr.Checkbox(
            label="The answer's factual claims are supported by its cited evidence",
            value=False
        )
        complete = gr.Checkbox(
            label="The answer sufficiently addresses the question",
            value=False
        )
        save_review_button = gr.Button("Save human evaluation")
        review_status = gr.Markdown()
        save_review_button.click(
            fn=save_review,
            inputs=[repo, quality_rating, grounded, complete],
            outputs=[review_status, history]
        )

    # Input and output connections.
    inputs = [
        repo,
        visibility,
        token,
        question,
        expected_relevant_files
    ]

    outputs = [
        answer,
        provenance,
        freshness,
        latency,
        evaluation,
        history,
        status
    ]

    # Show or hide the token field when visibility changes.
    visibility.change(
        fn=toggle_token_field,
        inputs=[visibility],
        outputs=[token, token_info]
    )

    # Analyze repository button.
    submit.click(
        fn=ask,
        inputs=inputs,
        outputs=outputs
    )

    # Submit using Enter in the question field.
    question.submit(
        fn=ask,
        inputs=inputs,
        outputs=outputs
    )


# --------------------------------------------------
# 8. APPLICATION ENTRY POINT
# --------------------------------------------------

if __name__ == "__main__":

    logger.info("Starting FreshTrust-MCP...")

    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        debug=True
    )
