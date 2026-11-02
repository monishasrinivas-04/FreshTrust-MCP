# FreshTrust-MCP Architecture

```mermaid
flowchart TD
    U[User / Gradio UI] --> V[Input Validation]
    V --> G[LangGraph StateGraph]
    G --> A[GitHub Authentication and Authorization]
    A --> M[GitHub MCP Tool Discovery and Retrieval]
    M --> C[SQLite TTL Cache]
    M --> E[Chunking and Sentence Transformer Embeddings]
    C --> E
    E --> F[Commit Metadata and Freshness Scoring]
    F --> R[Relevance + Freshness Reranking]
    R --> X[Context Refinement with Provenance]
    X --> L[Cloudflare Workers AI]
    L --> O[Grounded Answer + Metrics]
```

## Security data flow

```mermaid
flowchart TD
    B[Browser] -->|HTTPS| W[Gradio Backend]
    W -->|Request-scoped PAT| S[Credential Scope]
    S -->|Authorization header| MCP[GitHub MCP]
    MCP --> GH[GitHub Repository]
    W -->|Cloudflare API token from server environment| CF[Cloudflare Workers AI]
    W --> LOG[Redacted Application Logs]
```

The backend must read/use the GitHub token, so this is not end-to-end encryption. TLS protects network transport; the PAT is not persisted by the application.
