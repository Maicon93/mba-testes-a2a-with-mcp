"""Tasks A2A em memoria, com o estado MRTR pendente guardado a parte.

A Task publica (o que vai no wire A2A) e o `Pendencia` privado (requestState,
chave da elicitation, enum, request MCP original) ficam em objetos separados:
o requestState nunca e serializado para o cliente A2A.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
REJECTED = "TASK_STATE_REJECTED"
TERMINAIS = {COMPLETED, CANCELED, FAILED, REJECTED}


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class Pendencia:
    """O que o agente precisa para repetir o tools/call depois da pausa.

    `request_state` e opaco: guardado e ecoado byte a byte, nunca aberto.
    """

    argumentos: dict[str, Any]
    chave: str
    campo: str
    opcoes: list[str]
    request_state: str
    politica: str


@dataclass
class Tarefa:
    id: str
    context_id: str
    estado: str = SUBMITTED
    mensagem: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    trace_id: str | None = None
    pendencia: Pendencia | None = None
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    def transitar(self, estado: str, texto: str | None = None) -> None:
        if self.estado in TERMINAIS:
            raise RuntimeError(f"Task {self.id} ja esta em estado terminal ({self.estado})")
        self.estado = estado
        if texto is None:
            self.mensagem = None
        else:
            self.mensagem = {
                "messageId": novo_id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.historico.append(self.mensagem)
        if estado in TERMINAIS:
            self.pendencia = None

    def wire(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem:
            status["message"] = self.mensagem
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.historico,
            "artifacts": self.artifacts,
        }


class Repositorio:
    def __init__(self) -> None:
        self._tarefas: dict[str, Tarefa] = {}

    def criar(self, context_id: str | None) -> Tarefa:
        tarefa = Tarefa(id=novo_id("task"), context_id=context_id or novo_id("ctx"))
        self._tarefas[tarefa.id] = tarefa
        return tarefa

    def obter(self, task_id: str) -> Tarefa | None:
        return self._tarefas.get(task_id)
