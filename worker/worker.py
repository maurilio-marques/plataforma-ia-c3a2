"""
Worker de fila (reaproveitado do C1.A2).

Laco: pega uma tarefa da fila Redis -> chama o servico de inferencia
(com tempo-limite, retentativa e circuit breaker) -> grava o resultado.

Rastreamento: a mensagem traz o contexto do trace criado no gateway
("trace"). O worker EXTRAI esse contexto e abre o span "fila.consumir"
como filho, entao gateway -> fila -> worker -> inferencia aparece como UM
unico trace no Jaeger.

Falhas: se a inferencia nao responder nem com as retentativas, a tarefa
volta para a fila (espera progressiva: 2s, 4s...). Apos MAX_TENTATIVAS,
vai para a dead-letter.

Rodar local:  python worker.py
"""
import logging
import os
import time

import pybreaker
from opentelemetry import trace
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.propagate import extract

import fila
import resiliencia
import telemetria

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("worker")

URL_INFERENCIA = os.getenv("URL_INFERENCIA", "http://localhost:8001")
MAX_TENTATIVAS = int(os.getenv("MAX_TENTATIVAS", "3"))

tracer = telemetria.configurar("worker")
RequestsInstrumentor().instrument()


def processar(mensagem: dict) -> dict:
    tarefa_id = mensagem["id"]
    contexto_pai = extract(mensagem.get("trace", {}))

    with tracer.start_as_current_span("fila.consumir", context=contexto_pai,
                                      kind=trace.SpanKind.CONSUMER) as span:
        span.set_attribute("messaging.system", "redis")
        span.set_attribute("messaging.destination.name", fila.FILA_TAREFAS)
        span.set_attribute("tarefa.id", tarefa_id)

        tentativa = fila.incrementar_tentativa(tarefa_id)
        span.set_attribute("tarefa.tentativa", tentativa)
        inicio = time.time()
        try:
            resposta = resiliencia.chamar("POST", f"{URL_INFERENCIA}/predict",
                                          json={"texto": mensagem["texto"]})
            if resposta.status_code >= 400:
                # erro do proprio pedido (4xx): nao adianta tentar de novo
                resultado = {"status": "failed", "task_id": tarefa_id, "resultado": None,
                             "erro": f"HTTP {resposta.status_code}", "tentativas": tentativa}
                fila.guardar_resultado(tarefa_id, resultado)
                logger.warning("task_id=%s status=failed http=%s", tarefa_id, resposta.status_code)
                return resultado

            resultado = {
                "status": "completed",
                "task_id": tarefa_id,
                "resultado": resposta.json(),
                "tentativas": tentativa,
                "tempo_ms": round((time.time() - inicio) * 1000, 2),
            }
            fila.guardar_resultado(tarefa_id, resultado)
            logger.info("task_id=%s status=completed tentativas=%s", tarefa_id, tentativa)
            return resultado

        except (resiliencia.ErroTransitorio, pybreaker.CircuitBreakerError) as erro:
            motivo = type(erro).__name__
            span.record_exception(erro)
            span.set_status(trace.Status(trace.StatusCode.ERROR, motivo))

            if tentativa >= MAX_TENTATIVAS:
                fila.guardar_dead_letter(tarefa_id, motivo, tentativa)
                resultado = {"status": "dead_letter", "task_id": tarefa_id, "resultado": None,
                             "erro": motivo, "tentativas": tentativa}
                fila.guardar_resultado(tarefa_id, resultado)
                logger.error("task_id=%s status=dead_letter erro=%s", tarefa_id, motivo)
                return resultado

            resultado = {"status": "retrying", "task_id": tarefa_id, "resultado": None,
                         "erro": motivo, "tentativas": tentativa}
            fila.guardar_resultado(tarefa_id, resultado)
            espera = 2 ** tentativa  # espera progressiva antes de devolver a fila
            logger.warning("task_id=%s status=retrying tentativa=%s espera=%ss erro=%s",
                           tarefa_id, tentativa, espera, motivo)
            time.sleep(espera)
            fila.reenfileirar(mensagem)  # o contexto do trace vai junto
            return resultado


def main():
    logger.info("worker pronto, aguardando tarefas na fila '%s'", fila.FILA_TAREFAS)
    while True:
        try:
            mensagem = fila.proxima_tarefa(timeout=5)
        except Exception as erro:  # noqa: BLE001  (Redis fora do ar)
            logger.warning("fila indisponivel (%s); tentando de novo em 3s", type(erro).__name__)
            time.sleep(3)
            continue
        if mensagem is not None:
            processar(mensagem)


if __name__ == "__main__":
    main()
