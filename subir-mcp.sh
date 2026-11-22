#!/usr/bin/env bash
# Sobe o servidor MCP em http://127.0.0.1:7301/mcp (stderr = log de requests).
# Exige REQUEST_STATE_SECRET exportado; o servidor explica como gerar se faltar.
set -euo pipefail
cd "$(dirname "$0")/servidor-mcp"
exec uv run python servidor.py
