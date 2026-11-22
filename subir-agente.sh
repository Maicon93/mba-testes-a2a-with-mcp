#!/usr/bin/env bash
# Sobe o agente A2A em http://127.0.0.1:7300 (card em /.well-known/agent-card.json, JSON-RPC em /a2a).
set -euo pipefail
cd "$(dirname "$0")/agente"
exec uv run python agente.py
