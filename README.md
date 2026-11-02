# FreshTrust-MCP

Secure, adaptive and freshness-aware GitHub repository intelligence using MCP, LangGraph, semantic RAG and hosted inference.

## Important deployment notes

- The original Kaggle notebook uses local Qwen3-14B. The deployed path does not load model weights.
- The default hosted model is Cloudflare `@cf/qwen/qwen3.8-27b`, **not** Qwen3-14B. Verify account eligibility and model access before deployment.
- Cloudflare Workers AI documents a 10,000-Neuron/day free allocation; exceeding it fails on Workers Free rather than automatically billing. Model-specific eligibility can change. See the official pricing and model pages.
- Render Free web services sleep when idle and have ephemeral local storage. SQLite cache on Render is therefore demonstrative, not durable.
- GitHub MCP access and Cloudflare inference require user-configured credentials. Never commit them.

## Local setup (Windows PowerShell)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` locally. Set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`. Do not paste secrets into chat or commit `.env`.

```powershell
python app.py
```

Open the local Gradio URL printed in the terminal.

## Tests

```powershell
pytest -q
python -m compileall .
```

## VS Code

1. Open this folder in VS Code.
2. Press `Ctrl+Shift+P` → Python: Select Interpreter → choose `.venv`.
3. Open Terminal → New Terminal and activate `.venv`.
4. Run the setup and test commands above.

If PowerShell blocks activation, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that terminal only, then activate again.

## Deployment

1. Push the project to a GitHub repository.
2. Create a Render Web Service from that repository.
3. Use `render.yaml` or set build command `pip install -r requirements.txt` and start command `python app.py`.
4. Add the Cloudflare credentials in Render Environment.
5. Confirm that the account/model can serve a request under the free allocation before sharing the app.

No paid fallback is configured. HTTP 402/403/429 or provider errors stop generation with an explicit error.

## Architecture

User → Gradio → LangGraph StateGraph → input validation → GitHub MCP retrieval → chunking/embeddings → freshness scoring and combined ranking → context refinement → Cloudflare Workers AI → cited response.

## Security notes

- User GitHub PAT is request-scoped and not written to disk by the application.
- TLS is expected from the hosting platform and remote APIs.
- MCP tool names are allow-listed and repository owner/name are checked.
- AES-GCM helpers are included only for a future use case requiring encrypted persistence; default behavior does not persist PATs.
- This is not WhatsApp-style end-to-end encryption. The backend must use the GitHub credential to access the repository.
- Treat repository content as untrusted evidence; prompt injection cannot be solved by encryption alone.

## Known limitations to validate

- GitHub Copilot MCP tool names/schemas and account access can change. The adapter discovers tools and supports a constrained schema mapping; test against your account.
- Search-result parsing depends on the GitHub MCP response shape. Inspect and extend parsing if your MCP server returns a different format.
- Semantic embeddings may be memory-intensive on a free web host. The code falls back to lexical scoring if the embedding model cannot load; this is explicitly reported in latency metadata.
- Current implementation uses SQLite caching, not PostgreSQL/pgvector.
- Freshness is incorporated into a weighted ranking score, but the weights need empirical calibration.
- Private repository token forwarding to GitHub MCP depends on the endpoint's accepted authentication mechanism.
- End-to-end evaluation requires a labelled repository-question dataset; the included tests are smoke tests, not benchmark evidence.
