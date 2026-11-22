"""Servidor MCP da Central de Salas (Streamable HTTP, spec 2026-07-28).

Tres tools (listar_salas, consultar_disponibilidade, reservar_sala) e um resource
(politica://uso). A reserva em conflito usa MRTR: o resolver `escolha_de_sala`
devolve um `Elicit`, o SDK transforma isso em `input_required` com o
`requestState` selado (AES-256-GCM, chave de REQUEST_STATE_SECRET) e, no retry,
injeta a resposta do cliente na tool.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.request_state import RequestStateSecurity
from pydantic import BaseModel, Field, create_model

from log_requests import LogDeRequests

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"

FUSO = timezone(timedelta(hours=-3))
ABERTURA, FECHAMENTO = time(8, 0), time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3
TTL_REQUEST_STATE = 600.0  # 10 minutos

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


# ---------------------------------------------------------------- dados

SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID = {s["id"]: s for s in SALAS}
RESERVAS: list[dict[str, Any]] = []  # cada item guarda tambem `_ini`/`_fim` ja convertidos
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def _registrar(reserva: dict[str, Any]) -> None:
    """Guarda a reserva com o intervalo ja convertido, para nao reparsear a cada consulta."""
    # _instante aplica o fuso padrao a horarios sem offset, como na validacao.
    RESERVAS.append({**reserva, "_ini": _instante(reserva["inicio"]), "_fim": _instante(reserva["fim"])})


def _proximo_id() -> str:
    maior = max((int(r["id"].split("-")[1]) for r in RESERVAS), default=0)
    return f"res-{maior + 1:04d}"


# ---------------------------------------------------------------- regras

def _instante(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError as e:
        raise ToolError(f"Data invalida: {valor}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO)
    return dt


for _r in json.loads((DADOS / "reservas.json").read_text(encoding="utf-8")):
    _registrar(_r)


def _validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as validacoes de sala e de politica, na ordem do enunciado."""
    if sala not in SALAS_POR_ID:
        raise ToolError(f"Sala inexistente: {sala}")
    ini, fi = _instante(inicio), _instante(fim)
    if fi <= ini:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio")
    ini_local, fim_local = ini.astimezone(FUSO), fi.astimezone(FUSO)
    abertura = ini_local.replace(hour=ABERTURA.hour, minute=0, second=0, microsecond=0)
    fechamento = ini_local.replace(hour=FECHAMENTO.hour, minute=0, second=0, microsecond=0)
    if ini_local < abertura or fim_local > fechamento:
        raise ToolError("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")
    if fi - ini > DURACAO_MAXIMA:
        raise ToolError("Duracao acima do limite: a politica permite no maximo 2 horas")
    return ini, fi


def _conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict[str, Any]]:
    return [
        r for r in RESERVAS
        if r["sala"] == sala and r["_ini"] < fi and ini < r["_fim"]
    ]


def _alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    capacidade = SALAS_POR_ID[sala]["capacidade"]
    candidatas = [
        s for s in SALAS
        if s["id"] != sala and s["capacidade"] >= capacidade and not _conflitos(s["id"], ini, fi)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]


# ---------------------------------------------------------------- modelos de saida

class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# ---------------------------------------------------------------- servidor

def _seguranca() -> RequestStateSecurity:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if not segredo:
        raise SystemExit(
            "REQUEST_STATE_SECRET nao definido. Gere com:\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    # O codec do SDK recusa chaves com menos de 32 bytes.
    return RequestStateSecurity(keys=[segredo], ttl=TTL_REQUEST_STATE, bind_principal=None)


mcp = MCPServer("central-de-salas", version="1.0.0", request_state_security=_seguranca())


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = _validar(sala, inicio, fim)
    conflitos = _conflitos(sala, ini, fi)
    return Disponibilidade(
        sala=sala,
        livre=not conflitos,
        conflitos=[ConflitoOut(**r) for r in conflitos],
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[Any] | None:
    """Resolver do MRTR: so pergunta quando o intervalo esta ocupado.

    Roda de novo em cada rodada; a resposta do cliente so e aceita se a pergunta
    renderizar igual a que foi selada no requestState.
    """
    ini, fi = _validar(sala, inicio, fim)
    if not _conflitos(sala, ini, fi):
        return None
    alternativas = _alternativas(sala, ini, fi)
    if not alternativas:
        raise ToolError("Sem alternativas disponiveis no intervalo")
    Escolha = create_model(
        "Escolha",
        sala=(Literal[tuple(alternativas)], Field(title="Sala", description="Sala alternativa escolhida")),
    )
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", Escolha)


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if not isinstance(escolha, AcceptedElicitation):
        return ReservaOut(reservado=False, motivo="recusado")
    # O resolver ja validou o pedido e, se houve pergunta, re-renderizou a mesma
    # lista nesta rodada: a sala escolhida continua livre.
    destino = sala if escolha.data is None else escolha.data.sala
    reserva = {"id": _proximo_id(), "sala": destino, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    _registrar(reserva)
    return ReservaOut(
        reserva=reserva["id"], sala=destino, inicio=inicio, fim=fim,
        responsavel=responsavel, politica=POLITICA_VERSAO,
    )


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas. A primeira linha declara a versao.")
def politica() -> str:
    return POLITICA_TEXTO


def main() -> None:
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True)
    uvicorn.run(LogDeRequests(app), host=os.environ.get("MCP_HOST", "127.0.0.1"), port=porta, log_level="warning")


if __name__ == "__main__":
    main()
