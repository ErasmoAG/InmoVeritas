"""Clase base de los agentes: identidad, prompt propio, salida estructurada, reintentos,
tiempo limite, registro de ejecucion y manejo de errores. Los agentes con LLM usan
LangChain (ChatPromptTemplate | ChatOllama.with_structured_output)."""
from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional, Type, TypeVar

from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama
from pydantic import BaseModel

from schemas.schemas import AgentLogEntry
from settings import Settings, get_settings

T = TypeVar("T", bound=BaseModel)

_llm_cache: dict[tuple, ChatOllama] = {}


def get_llm(settings: Optional[Settings] = None, num_predict: int = 1400) -> ChatOllama:
    s = settings or get_settings()
    key = (s.ollama_base_url, s.ollama_model, num_predict, s.llm_timeout_seconds)
    if key not in _llm_cache:
        _llm_cache[key] = ChatOllama(model=s.ollama_model, base_url=s.ollama_base_url, temperature=0, num_ctx=8192,
                                     num_predict=num_predict, client_kwargs={"timeout": s.llm_timeout_seconds})
    return _llm_cache[key]


def squash(text: str) -> str:
    """Normaliza para comparar: minusculas, sin acentos, sin espacios ni signos."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", t)


class BaseAgent:
    agent_id = "A0"
    name = "Agente base"
    responsibility = ""
    system_prompt = ""
    uses_llm = True

    def __init__(self, settings: Optional[Settings] = None, retries: int = 1):
        self.settings = settings or get_settings()
        self.retries = retries
        self.log: list[AgentLogEntry] = []
        self._retries_used = 0

    def invoke_llm(self, schema: Type[T], human: str, **vars) -> T:
        """Llamada LangChain con salida estructurada; reintenta si el modelo falla o devuelve algo invalido."""
        prompt = ChatPromptTemplate.from_messages([("system", self.system_prompt), ("human", human)])
        chain = prompt | get_llm(self.settings).with_structured_output(schema, method="json_schema")
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                out = chain.invoke(vars)
                if out is None:
                    raise ValueError("El modelo no devolvio una estructura valida")
                self._retries_used = attempt
                return out
            except Exception as exc:  # timeout, JSON invalido, Ollama caido...
                last = exc
        raise RuntimeError(f"{self.name}: fallo la llamada al modelo ({type(last).__name__}: {str(last)[:160]})")

    def record(self, t0: float, started: datetime, status: str, input_summary: str = "", output_summary: str = "",
               error: Optional[str] = None, used_llm: Optional[bool] = None) -> AgentLogEntry:
        entry = AgentLogEntry(agent_id=self.agent_id, agent_name=self.name, started_at=started,
                              duration_ms=int((time.monotonic() - t0) * 1000), status=status,
                              input_summary=input_summary[:200], output_summary=output_summary[:300], error=error,
                              retries=self._retries_used, used_llm=self.uses_llm if used_llm is None else used_llm)
        self.log.append(entry)
        return entry

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
