"""
Servico de inferencia (reaproveitado do C1.A2) + armazenamento vetorial.

Nao e exposto na internet: so o gateway e o worker chamam este servico,
pela rede interna do docker-compose (pelo nome "inferencia").

Rotas:
  GET  /saude
  POST /predict     classifica o sentimento e guarda o vetor no Qdrant
  POST /similares   busca os textos mais parecidos no Qdrant

Rodar local:  uvicorn app:app --port 8001
"""
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from pydantic import BaseModel, Field

import telemetria
import vetorial
from modelo import carregar_modelo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("inferencia")

tracer = telemetria.configurar("inferencia")
modelo = None


@asynccontextmanager
async def ciclo_de_vida(_app: FastAPI):
    """Carrega o modelo UMA vez na subida (licao do C1.A2)."""
    global modelo
    inicio = time.time()
    modelo = carregar_modelo()
    logger.info("modelo carregado em %.3fs", time.time() - inicio)
    vetorial.iniciar_limpeza_periodica()
    yield


app = FastAPI(title="Servico de Inferencia (C3.A2)", version="1.0.0", lifespan=ciclo_de_vida)
FastAPIInstrumentor.instrument_app(app, excluded_urls="saude", exclude_spans=["receive", "send"])
RequestsInstrumentor().instrument()  # chamadas ao Qdrant aparecem no trace


class Entrada(BaseModel):
    texto: str = Field(min_length=1, max_length=5000)


class Busca(BaseModel):
    texto: str = Field(min_length=1, max_length=5000)
    limite: int = Field(default=3, ge=1, le=10)


@app.middleware("http")
async def registrar(request: Request, call_next):
    inicio = time.time()
    resposta = await call_next(request)
    logger.info("%s %s -> %s (%.1fms)", request.method, request.url.path,
                resposta.status_code, (time.time() - inicio) * 1000)
    return resposta


@app.get("/saude")
def saude():
    return {"status": "ok", "modelo_carregado": modelo is not None}


@app.post("/predict")
def predict(entrada: Entrada):
    texto = entrada.texto.strip()
    if not texto:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "texto vazio")

    inicio = time.time()
    with tracer.start_as_current_span("modelo.prever") as span:
        resultado = modelo.prever(texto)
        span.set_attribute("modelo.sentimento", resultado["sentimento"])
    resultado["tempo_ms"] = round((time.time() - inicio) * 1000, 2)

    # Degradacao graciosa: se o Qdrant estiver fora, a inferencia continua
    # respondendo; so o historico vetorial deixa de ser gravado.
    try:
        vetorial.salvar(texto, resultado)
        resultado["vetor_armazenado"] = True
    except Exception as erro:  # noqa: BLE001
        logger.warning("falha ao gravar no armazenamento vetorial: %s", type(erro).__name__)
        trace.get_current_span().set_attribute("vetorial.erro", type(erro).__name__)
        resultado["vetor_armazenado"] = False
    return resultado


@app.post("/similares")
def similares(busca: Busca):
    try:
        return {"consulta": busca.texto, "similares": vetorial.buscar(busca.texto, busca.limite)}
    except Exception as erro:  # noqa: BLE001
        logger.warning("falha na busca vetorial: %s", type(erro).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "armazenamento vetorial indisponivel")
