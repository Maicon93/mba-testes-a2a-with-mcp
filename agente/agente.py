"""Agente Central de Salas: servidor A2A v1.0 por fora, host MCP por dentro.

Nao tem LLM nem regra de sala. Interpreta um pedido em formato fixo, chama o
servidor MCP e traduz o resultado em estado de Task A2A. A ponte entre os dois
protocolos esta em `_aplicar_resultado` (input_required -> TASK_STATE_INPUT_REQUIRED)
e em `_continuar` (requestState guardado -> retry do tools/call com id novo).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from mcp_types import INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_cliente import ClienteMCP, ErroMCP, novo_traceparent
from tarefas import (
    CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, TERMINAIS, WORKING, Pendencia, Repositorio, Tarefa,
)

PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
_HOST_PUBLICO = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://{_HOST_PUBLICO}:{PORTA}").rstrip("/")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"
CAMPOS_RESERVA = ("reserva", "sala", "inicio", "fim", "responsavel")

# `reservar chave=valor ...`: os nomes aceitos vem do inputSchema descoberto, nao daqui.
# O valor vai ate o proximo ` chave=`, entao o ultimo pode ter espacos (responsavel).
PEDIDO = re.compile(r"^reservar\s+(?P<resto>.+)$")
PAR = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|\s*$)")
ESCOLHA = re.compile(r"^escolha=(?P<valor>\S+)\s*$")
TRACEPARENT = re.compile(r"^[0-9a-f]{2}-(?P<trace>[0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")

# Erros JSON-RPC especificos do A2A v1.0
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004

CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
    ],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}

repo = Repositorio()
mcp = ClienteMCP(MCP_URL)


class ErroA2A(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _texto(mensagem: dict[str, Any]) -> str:
    return " ".join(p.get("text", "") for p in mensagem.get("parts", []) if isinstance(p, dict)).strip()


def _trace_id(request: Request) -> str | None:
    m = TRACEPARENT.match(request.headers.get("traceparent", "").strip().lower())
    return m.group("trace") if m else None


def _log(tarefa: Tarefa, evento: str) -> None:
    print(f"[agente] task={tarefa.id} estado={tarefa.estado} {evento}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- host MCP

async def _descobrir(tarefa: Tarefa) -> tuple[dict[str, Any], str]:
    """tools/list e leitura da politica, em paralelo, antes de qualquer tools/call da Task."""
    tools, texto = await asyncio.gather(
        mcp.listar_tools(novo_traceparent(tarefa.trace_id)),
        mcp.ler_resource(URI_POLITICA, novo_traceparent(tarefa.trace_id)),
    )
    tool = next((t for t in tools if t.get("name") == TOOL_RESERVA), None)
    if tool is None:
        raise ErroMCP(METHOD_NOT_FOUND, f"o servidor MCP nao oferece a tool {TOOL_RESERVA}")
    primeira = texto.splitlines()[0] if texto else ""
    versao = primeira.split(":", 1)[1].strip() if primeira.startswith("versao:") else ""
    return tool, versao


def _argumentos(tool: dict[str, Any], pedido: dict[str, str]) -> dict[str, Any]:
    """Monta os argumentos a partir do inputSchema descoberto, nao de uma lista fixa."""
    schema = tool.get("inputSchema") or {}
    propriedades = schema.get("properties") or {}
    faltando = [c for c in schema.get("required", []) if c not in pedido]
    if faltando:
        raise ValueError(f"o pedido nao traz os campos exigidos pela tool: {', '.join(faltando)}")
    return {k: v for k, v in pedido.items() if k in propriedades}


# ---------------------------------------------------------------- a ponte

def _aplicar_resultado(tarefa: Tarefa, argumentos: dict[str, Any], resultado: dict[str, Any], politica: str) -> None:
    """Traduz o resultado de um tools/call em estado de Task."""
    if resultado.get("resultType") == "input_required":
        # PONTE (ida): input_required do MCP vira TASK_STATE_INPUT_REQUIRED no A2A.
        pedidos = resultado.get("inputRequests") or {}
        if len(pedidos) != 1:
            tarefa.transitar(FAILED, f"input_required inesperado: {len(pedidos)} pedidos")
            return
        chave, pedido = next(iter(pedidos.items()))
        propriedades = ((pedido.get("params") or {}).get("requestedSchema") or {}).get("properties") or {}
        if len(propriedades) != 1 or not resultado.get("requestState"):
            tarefa.transitar(FAILED, "input_required malformado: esperado um campo e um requestState")
            return
        # O schema e plano com um unico campo; o nome dele vem do servidor, nao daqui.
        (campo, definicao), = propriedades.items()
        # Com uma alternativa so, o gerador de schema do SDK emite `const` em vez de `enum`.
        opcoes = list(definicao.get("enum") or ([definicao["const"]] if "const" in definicao else []))
        tarefa.pendencia = Pendencia(
            argumentos=argumentos, chave=chave, campo=campo, opcoes=opcoes,
            request_state=resultado["requestState"], politica=politica,
        )
        tarefa.transitar(INPUT_REQUIRED, "alternativas: " + ", ".join(opcoes))
        return

    if resultado.get("isError"):
        mensagem = " ".join(c.get("text", "") for c in resultado.get("content", []) if c.get("type") == "text")
        tarefa.transitar(FAILED, mensagem)
        return

    dados = resultado.get("structuredContent") or {}
    if not dados:
        tarefa.transitar(FAILED, "resultado da tool sem structuredContent")
        return
    if dados.get("reservado") is False:
        tarefa.transitar(CANCELED, f"Reserva nao realizada: {dados.get('motivo') or 'recusada'}.")
        return

    reserva = {k: dados.get(k) for k in CAMPOS_RESERVA} | {"politica": politica}
    tarefa.artifacts.append({
        "artifactId": f"art-{tarefa.id.split('-', 1)[1]}",
        "name": "reserva",
        "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}],
    })
    tarefa.transitar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


async def _iniciar(tarefa: Tarefa, texto: str) -> None:
    tarefa.transitar(WORKING)
    m = PEDIDO.match(texto)
    if not m:
        tarefa.transitar(FAILED, "Pedido invalido. Formato: reservar sala=<id> inicio=<iso8601> "
                                 "fim=<iso8601> responsavel=<nome>")
        return
    try:
        tool, politica = await _descobrir(tarefa)
        argumentos = _argumentos(tool, dict(PAR.findall(m.group("resto"))))
        resultado = await mcp.chamar_tool(TOOL_RESERVA, argumentos, novo_traceparent(tarefa.trace_id))
        _aplicar_resultado(tarefa, argumentos, resultado, politica)
    except Exception as e:  # qualquer falha leva a Task a um estado terminal
        _falhar(tarefa, e)


async def _continuar(tarefa: Tarefa, texto: str) -> None:
    pendencia = tarefa.pendencia
    assert pendencia is not None
    m = ESCOLHA.match(texto)
    valor = m.group("valor") if m else None

    if valor == "recusar":
        resposta: dict[str, Any] = {"action": "decline"}
    elif valor in pendencia.opcoes:
        resposta = {"action": "accept", "content": {pendencia.campo: valor}}
    else:
        # Escolha fora do enum: continua pausada e repete a mesma lista.
        tarefa.transitar(INPUT_REQUIRED, "alternativas: " + ", ".join(pendencia.opcoes))
        return

    tarefa.transitar(WORKING)
    tarefa.pendencia = None  # o requestState e de uso unico: a resposta abaixo decide o proximo passo
    try:
        # PONTE (volta): o mesmo tools/call, com id novo, levando a resposta sob a
        # mesma chave e o requestState ecoado sem modificacao.
        resultado = await mcp.chamar_tool(
            TOOL_RESERVA, pendencia.argumentos, novo_traceparent(tarefa.trace_id),
            input_responses={pendencia.chave: resposta},
            request_state=pendencia.request_state,
        )
        _aplicar_resultado(tarefa, pendencia.argumentos, resultado, pendencia.politica)
    except Exception as e:  # qualquer falha leva a Task a um estado terminal
        _falhar(tarefa, e)


def _falhar(tarefa: Tarefa, erro: Exception) -> None:
    if isinstance(erro, (ErroMCP, ValueError)):
        mensagem = str(erro)
    else:
        print(f"[agente] task={tarefa.id} erro inesperado: {erro!r}", file=sys.stderr, flush=True)
        mensagem = f"Falha ao processar a resposta do servidor MCP: {type(erro).__name__}"
    if tarefa.estado not in TERMINAIS:
        tarefa.transitar(FAILED, mensagem)


# ---------------------------------------------------------------- servidor A2A

async def send_message(params: dict[str, Any], request: Request) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not mensagem.get("parts"):
        raise ErroA2A(INVALID_PARAMS, "params.message com parts e obrigatorio")
    texto = _texto(mensagem)
    mensagem = dict(mensagem)
    trace_id = _trace_id(request)

    task_id = mensagem.get("taskId")
    if task_id:
        tarefa = repo.obter(task_id)
        if tarefa is None:
            raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
        async with tarefa.trava:
            # Cobre os estados terminais: so uma Task pausada aceita continuacao.
            if tarefa.estado != INPUT_REQUIRED or tarefa.pendencia is None:
                raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} nao aceita mensagens no estado {tarefa.estado}")
            tarefa.trace_id = tarefa.trace_id or trace_id
            mensagem.setdefault("contextId", tarefa.context_id)
            tarefa.historico.append(mensagem)
            await _continuar(tarefa, texto)
            _log(tarefa, "continuacao")
            return {"task": tarefa.wire()}

    tarefa = repo.criar(mensagem.get("contextId"))
    tarefa.trace_id = trace_id
    async with tarefa.trava:
        mensagem["taskId"] = tarefa.id
        mensagem.setdefault("contextId", tarefa.context_id)
        tarefa.historico.append(mensagem)
        await _iniciar(tarefa, texto)
        _log(tarefa, "pedido")
        return {"task": tarefa.wire()}


async def get_task(params: dict[str, Any], _request: Request) -> dict[str, Any]:
    tarefa = repo.obter(str(params.get("id", "")))
    if tarefa is None:
        raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {params.get('id')}")
    return {"task": tarefa.wire()}


METODOS = {"SendMessage": send_message, "GetTask": get_task}


def _erro(id_: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


async def a2a(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _erro(None, PARSE_ERROR, "Parse error")
    id_ = corpo.get("id") if isinstance(corpo, dict) else None
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return _erro(id_, INVALID_REQUEST, "Invalid Request")
    metodo = METODOS.get(corpo["method"])
    if metodo is None:
        return _erro(id_, METHOD_NOT_FOUND, f"Method not found: {corpo['method']}")
    params = corpo.get("params") if isinstance(corpo.get("params"), dict) else {}
    try:
        resultado = await metodo(params, request)
    except ErroA2A as e:
        return _erro(id_, e.code, str(e))
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})


async def agent_card(_request: Request) -> JSONResponse:
    return JSONResponse(CARD)


@asynccontextmanager
async def ciclo_de_vida(_app: Starlette):
    yield
    await mcp.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
