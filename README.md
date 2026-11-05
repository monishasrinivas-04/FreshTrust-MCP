FRESHTRUST-MCP REPLACEMENT FILES

Copy these five files into the matching locations in your existing project:
- app.py
- agents/orchestrator.py
- mcp_layer/github_client.py
- rag/retriever.py
- config/settings.py

The uploaded app.py is preserved because it already has the correct UI:
Public = URL + question only.
Private = URL + question + user-entered PAT.

.env contains application configuration only:
GITHUB_MCP_URL=https://api.githubcopilot.com/mcp/
CLOUDFLARE_ACCOUNT_ID=...
CLOUDFLARE_API_TOKEN=...
CLOUDFLARE_MODEL=...

Remove any GITHUB_MCP_TOKEN line from .env. The application does not read a
developer-owned GitHub token. A private user's PAT is passed to the MCP client
for that request only. It is not stored in LangGraph state, cache, prompt, log,
or returned result. Commit metadata is obtained using the MCP list_commits tool;
the RAG retriever contains no GitHub REST calls.

IMPORTANT PUBLIC-ENDPOINT LIMITATION:
The public workflow does not ask the user for a PAT. However, if the configured
remote MCP endpoint returns HTTP 401 without an Authorization header, that
endpoint is enforcing gateway authentication even for public repositories.
This code intentionally does not hide that limitation by using a global
developer token. To meet the no-prompt public workflow, configure an MCP
gateway that permits anonymous public reads or implement end-user OAuth.
For private repositories, the UI-supplied PAT is sent as Authorization: Bearer.

GitHub's remote MCP documentation describes the Bearer PAT header:
https://docs.github.com/en/copilot/how-tos/copilot-in-your-ide/customize-copilot/extend-copilot-with-tools-and-context/set-up-the-github-mcp-server

Run from the project root:
python app.py

This is a source replacement bundle; no claim is made that it has been run
against your local environment. Keep your existing requirements.txt and
other modules unchanged.

FRESHTRUST-MCP — MCP-ONLY CHANGE SET

Replace/add ONLY these files:
- mcp_layer/github_client.py       (replace)
- agents/orchestrator.py           (replace)
- rag/retriever.py                 (replace; only commit-metadata source changed)
- security/request_secret.py       (new)

Leave app.py, Gradio UI, Cloudflare LLM, embeddings, chunking, reranking,
context refinement, cache, evaluation, settings, and requirements unchanged.

BEHAVIOUR
Public repository:
- User enters repository URL and question.
- No PAT is requested or sent.
- No GITHUB_MCP_TOKEN or shared server-side GitHub token is read.

Private repository:
- User enters URL, question, and PAT using the existing UI.
- PAT is encrypted with AES-256-GCM in a request-local envelope.
- It is not included in LangGraph state and is not written to disk, cache,
  prompts, logs, or returned results.
- It is decrypted only when the retrieval node needs to establish the MCP
  connection. The MCP Authorization header is sent over HTTPS.

MCP / LANGGRAPH
- Repository file search and retrieval use GitHub MCP tools only.
- Commit metadata for freshness is retrieved through MCP list_commits.
- No direct GitHub REST calls remain in the replaced retriever.
- Existing LangGraph node sequence and RAG ranking/embedding logic are retained.
- Existing rate-limit cache fallback is retained.

IMPORTANT PUBLIC ENDPOINT LIMITATION
Your previous call to https://api.githubcopilot.com/mcp/ returned 401 when no
Authorization header was sent. This replacement intentionally sends no token
for public repositories, exactly as requested. Therefore the MCP endpoint
configured by GITHUB_MCP_URL must permit anonymous public reads. If that
endpoint continues to return 401, use an MCP server/gateway configured to allow
anonymous public repository access. Do not add a shared developer token.

No new .env variable is needed. Keep GITHUB_MCP_URL and your existing Cloudflare
configuration. Remove GITHUB_MCP_TOKEN if it exists.

Run normally:
    python app.py

No diagnostic or smoke-test commands are included. This is a source replacement
package and has not been executed against your live MCP endpoint.
