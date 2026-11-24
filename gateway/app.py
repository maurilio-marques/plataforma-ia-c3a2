"""
API Gateway - porta de entrada UNICA da plataforma (unico servico publico).

Rotas:
  GET  /saude              publica, usada pelo healthcheck
  POST /login              publica, devolve o token JWT
  POST /predict            protegida, inferencia SINCRONA (gateway -> inferencia via REST)
  POST /tarefas            protegida, inferencia ASSINCRONA (gateway -> fila Redis -> worker)
  GET  /tarefas/{id}       protegida, consulta o resultado da tarefa
  POST /similares          protegida, busca textos parecidos no armazenamento vetorial

Rodar local:  uvicorn app:app --port 8000
"""
import json
import logging
import os
import time
import uuid

import pybreaker
import redis
from fastapi import Depends, FastAPI, HTTPException, Request, status
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagate import inject
from pydantic import BaseModel, Field

import resiliencia
import seguranca
import telemetria

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("gateway")

URL_INFERENCIA = os.getenv("URL_INFERENCIA", "http://localhost:8001")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
FILA_TAREFAS = os.getenv("FILA_TAREFAS", "tarefas")
RETENCAO_RESULTADO_SEGUNDOS = int(os.getenv("RETENCAO_RESULTADO_SEGUNDOS", "86400"))

tracer = telemetria.configurar("gateway")
app = FastAPI(title="Gateway - Plataforma de IA como Servico (C3.A2)", version="1.0.0")
FastAPIInstrumentor.instrument_app(app, excluded_urls="saude", exclude_spans=["receive", "send"])
RequestsInstrumentor().instrument()  # propaga o trace nas chamadas HTTP para a inferencia

redis_cliente = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)


# ---------- modelos de entrada ----------
class Login(BaseModel):
    usuario: str
    senha: str


class Entrada(BaseModel):
    texto: str = Field(min_length=1, max_length=5000)


class Busca(BaseModel):
    texto: str = Field(min_length=1, max_length=5000)
    limite: int = Field(default=3, ge=1, le=10)


# ---------- log de requisicoes (sem conteudo: LGPD) ----------
@app.middleware("http")
async def registrar(request: Request, call_next):
    inicio = time.time()
    resposta = await call_next(request)
    ms = (time.time() - inicio) * 1000
    trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")
    logger.info("%s %s -> %s (%.1fms) trace_id=%s",
                request.method, request.url.path, resposta.status_code, ms, trace_id)
    return resposta


def protegido(usuario: str = Depends(seguranca.usuario_autenticado)) -> str:
    """JWT valido + rate limit por usuario."""
    seguranca.aplicar_rate_limit(redis_cliente, f"usuario:{usuario}")
    return usuario


def _repassar(circuito: str, metodo: str, caminho: str, corpo: dict) -> dict:
    """Chama a inferencia com tempo-limite, retentativa e circuit breaker."""
    try:
        resposta = resiliencia.chamar(circuito, metodo, f"{URL_INFERENCIA}{caminho}", json=corpo)
    except pybreaker.CircuitBreakerError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"servico indisponivel (circuito '{circuito}' aberto), tente mais tarde")
    except resiliencia.ErroTransitorio as erro:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"servico indisponivel apos retentativas ({erro})")
    if resposta.status_code >= 400:
        raise HTTPException(resposta.status_code, resposta.json().get("detail", "erro na inferencia"))
    return resposta.json()


# ---------- rotas ----------
@app.get("/saude")
def saude():
    return {"status": "ok",
            "circuitos": {nome: d.current_state for nome, d in resiliencia.disjuntores.items()}}


@app.post("/login")
def login(dados: Login, request: Request):
    ip = request.client.host if request.client else "desconhecido"
    seguranca.aplicar_rate_limit(redis_cliente, f"login:{ip}")  # freia forca bruta
    if not seguranca.credenciais_validas(dados.usuario, dados.senha):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "usuario ou senha invalidos")
    return {
        "access_token": seguranca.emitir_token(dados.usuario),
        "token_type": "bearer",
        "expira_em_minutos": seguranca.JWT_EXPIRA_MINUTOS,
    }


@app.post("/predict")
def predict(entrada: Entrada, usuario: str = Depends(protegido)):
    """Inferencia sincrona: o gateway roteia para o servico de inferencia."""
    return _repassar("predict", "POST", "/predict", entrada.model_dump())


@app.post("/tarefas", status_code=status.HTTP_202_ACCEPTED)
def criar_tarefa(entrada: Entrada, usuario: str = Depends(protegido)):
    """Inferencia assincrona: publica na fila e devolve o id na hora."""
    tarefa_id = str(uuid.uuid4())
    with tracer.start_as_current_span("fila.publicar", kind=trace.SpanKind.PRODUCER) as span:
        span.set_attribute("messaging.system", "redis")
        span.set_attribute("messaging.destination.name", FILA_TAREFAS)
        span.set_attribute("tarefa.id", tarefa_id)

        # Propagacao do contexto: o trace viaja DENTRO da mensagem,
        # e o worker continua o mesmo trace do outro lado da fila.
        contexto = {}
        inject(contexto)
        mensagem = {"id": tarefa_id, "texto": entrada.texto, "trace": contexto}
        try:
            pipe = redis_cliente.pipeline()
            pipe.set(f"resultado:{tarefa_id}",
                     json.dumps({"status": "queued", "task_id": tarefa_id, "resultado": None}),
                     ex=RETENCAO_RESULTADO_SEGUNDOS)
            pipe.rpush(FILA_TAREFAS, json.dumps(mensagem))
            pipe.execute()
        except redis.RedisError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "fila indisponivel")
    return {"id": tarefa_id, "status": "queued", "consultar_em": f"/tarefas/{tarefa_id}"}


@app.get("/tarefas/{tarefa_id}")
def consultar_tarefa(tarefa_id: str, usuario: str = Depends(protegido)):
    try:
        bruto = redis_cliente.get(f"resultado:{tarefa_id}")
    except redis.RedisError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "armazenamento de resultados indisponivel")
    if bruto is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tarefa inexistente ou expirada")
    return json.loads(bruto)


@app.post("/similares")
def similares(busca: Busca, usuario: str = Depends(protegido)):
    """Busca semantica no armazenamento vetorial (via servico de inferencia)."""
    return _repassar("similares", "POST", "/similares", busca.model_dump())
