"""Cliente MCP minimo sobre Streamable HTTP (spec 2026-07-28), sem sessao.

Cada request e autocontido: leva no `_meta` a versao do protocolo, o clientInfo
e as capabilities, e nos headers o espelho que o transporte exige
(MCP-Protocol-Version, Mcp-Method, Mcp-Name). Nada e inferido de request
anterior. Nomes de chaves e headers vem do proprio SDK.

Por que nao o `mcp.client.Client` do SDK: ele declara `elicitation` com `form` e
`url` (o desafio pede so `form`), carimba o proprio `_meta` por cima do nosso e,
com um callback de elicitation, fecha o ciclo de MRTR sozinho. O agente precisa
enxergar o `input_required` cru para pausar a Task A2A.
"""

from __future__ import annotations

import secrets
import sys
from typing import Any

import httpx
from mcp.shared.inbound import (
    MCP_METHOD_HEADER,
    MCP_NAME_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    NAME_BEARING_METHODS,
    encode_header_value,
)
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    INTERNAL_ERROR,
    PROTOCOL_VERSION_META_KEY,
)

# Fixado na revisao exigida pelo desafio, nao na mais nova que o SDK conhece.
PROTOCOLO = "2026-07-28"
CAPABILITIES = {"elicitation": {"form": {}}}
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}


class ErroMCP(Exception):
    """Erro de protocolo (JSON-RPC `error`) devolvido pelo servidor MCP."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{message} (codigo {code})")
        self.code = code


def novo_traceparent(trace_id: str | None) -> str | None:
    """Mesmo trace-id, span-id novo a cada request."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01" if trace_id else None


class ClienteMCP:
    def __init__(self, url: str) -> None:
        self.url = url
        self._http = httpx.AsyncClient(timeout=30.0)

    async def fechar(self) -> None:
        await self._http.aclose()

    async def request(self, metodo: str, params: dict[str, Any] | None = None,
                      traceparent: str | None = None) -> dict[str, Any]:
        """Envia um request com id novo e devolve o `result` cru."""
        params = dict(params or {})
        meta: dict[str, Any] = {
            PROTOCOL_VERSION_META_KEY: PROTOCOLO,
            CLIENT_INFO_META_KEY: CLIENT_INFO,
            CLIENT_CAPABILITIES_META_KEY: CAPABILITIES,
        }
        if traceparent:
            meta["traceparent"] = traceparent
        params["_meta"] = meta

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            MCP_PROTOCOL_VERSION_HEADER: PROTOCOLO,
            MCP_METHOD_HEADER: metodo,
        }
        if metodo in NAME_BEARING_METHODS:
            headers[MCP_NAME_HEADER] = encode_header_value(str(params[NAME_BEARING_METHODS[metodo]]))

        # Id novo em todo request, inclusive no retry do MRTR: sao requests independentes.
        corpo = {"jsonrpc": "2.0", "id": f"agente-{secrets.token_hex(6)}", "method": metodo, "params": params}
        print(f"[agente->mcp] method={metodo} id={corpo['id']} traceparent={traceparent or '-'}",
              file=sys.stderr, flush=True)
        try:
            resposta = await self._http.post(self.url, json=corpo, headers=headers)
        except httpx.HTTPError as e:
            raise ErroMCP(INTERNAL_ERROR, f"servidor MCP inacessivel: {e!r}") from e
        try:
            dados = resposta.json()
        except ValueError as e:
            raise ErroMCP(INTERNAL_ERROR, f"resposta nao-JSON do servidor MCP (HTTP {resposta.status_code})") from e
        if not isinstance(dados, dict):
            raise ErroMCP(INTERNAL_ERROR, "resposta do servidor MCP nao e um objeto JSON-RPC")
        if "error" in dados:
            erro = dados["error"]
            raise ErroMCP(erro.get("code", INTERNAL_ERROR), erro.get("message", ""))
        return dados.get("result") or {}

    async def listar_tools(self, traceparent: str | None) -> list[dict[str, Any]]:
        return (await self.request("tools/list", {}, traceparent)).get("tools", [])

    async def ler_resource(self, uri: str, traceparent: str | None) -> str:
        resultado = await self.request("resources/read", {"uri": uri}, traceparent)
        return "".join(c.get("text", "") for c in resultado.get("contents", []))

    async def chamar_tool(self, nome: str, argumentos: dict[str, Any], traceparent: str | None, *,
                          input_responses: dict[str, Any] | None = None,
                          request_state: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state
        return await self.request("tools/call", params, traceparent)
