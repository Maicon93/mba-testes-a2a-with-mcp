"""Middleware ASGI que registra no stderr cada request JSON-RPC recebido.

Fica fora do SDK de proposito: registra tambem os requests que o SDK recusa
antes de chegar na tool (por exemplo, _meta incompleto).
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _registrar(corpo: bytes) -> None:
    try:
        dados = json.loads(corpo or b"null")
    except json.JSONDecodeError:
        print("[mcp] corpo nao e JSON", file=sys.stderr, flush=True)
        return
    for msg in dados if isinstance(dados, list) else [dados]:
        if not isinstance(msg, dict):
            continue
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        nome = params.get("name") or params.get("uri")
        partes = [f"method={msg.get('method')}", f"id={msg.get('id')}"]
        if nome:
            partes.append(f"name={nome}")
        partes.append(f"traceparent={meta.get('traceparent', '-')}")
        if "requestState" in params:
            partes.append("retry=sim")
        print("[mcp] " + " ".join(partes), file=sys.stderr, flush=True)


class LogDeRequests:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        pedacos: list[bytes] = []
        while True:
            evento = await receive()
            pedacos.append(evento.get("body", b""))
            if not evento.get("more_body"):
                break
        corpo = b"".join(pedacos)
        _registrar(corpo)

        entregue = False

        async def replay() -> dict:
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
