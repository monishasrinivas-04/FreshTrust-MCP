# Build the official GitHub MCP Server as a native executable.
# The current upstream go.mod requires Go 1.25.12.
FROM golang:1.27.3-alpine AS github-mcp-build

RUN apk add --no-cache git ca-certificates

ARG GITHUB_MCP_REF=main
WORKDIR /src

RUN git clone https://github.com/github/github-mcp-server.git .
RUN git checkout "${GITHUB_MCP_REF}"
RUN go build -trimpath -o /out/github-mcp-server ./cmd/github-mcp-server


# Python application runtime. No Docker daemon is required at runtime.
FROM python:3.12-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GITHUB_MCP_BINARY_PATH=/usr/local/bin/github-mcp-server

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY --from=github-mcp-build /out/github-mcp-server /usr/local/bin/github-mcp-server
RUN chmod 0755 /usr/local/bin/github-mcp-server

COPY . .

EXPOSE 7860

CMD ["python", "app.py"]
