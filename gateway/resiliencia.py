"""
Resiliencia nas chamadas do gateway para o servico de inferencia.

Tres mecanismos, do mais interno para o mais externo:
  1. Tempo-limite: nenhuma chamada espera mais que TIMEOUT_SEGUNDOS.
  2. Retentativa com espera progressiva (backoff exponencial): falhas
     transitorias (conexao, timeout, HTTP 5xx) sao repetidas ate
     RETRY_TENTATIVAS vezes, esperando 0,5s, 1s, 2s... entre elas.
  3. Circuit breaker: depois de CB_FALHAS_MAX chamadas que falharam mesmo
     com retentativa, o circuito ABRE e as proximas chamadas falham na hora
     (sem bater no servico doente) por CB_RESET_SEGUNDOS. Depois disso,
     uma chamada de teste e liberada (meio-aberto); se der certo, fecha.

Erros 4xx NAO contam como falha: o problema foi do cliente, nao do servico.

Cada rota tem o SEU circuito: se o armazenamento vetorial cair, so
/similares abre o circuito; /predict continua funcionando (isolamento de
falhas, padrao "bulkhead").
"""
import logging
import os

import pybreaker
import requests
from tenacity import (before_sleep_log, retry, retry_if_exception_type,
                      stop_after_attempt, wait_exponential)

logger = logging.getLogger("gateway.resiliencia")

TIMEOUT_SEGUNDOS = float(os.getenv("TIMEOUT_SEGUNDOS", "3"))
RETRY_TENTATIVAS = int(os.getenv("RETRY_TENTATIVAS", "3"))
CB_FALHAS_MAX = int(os.getenv("CB_FALHAS_MAX", "5"))
CB_RESET_SEGUNDOS = int(os.getenv("CB_RESET_SEGUNDOS", "30"))


class ErroTransitorio(Exception):
    """Falha que vale a pena tentar de novo (rede, timeout, 5xx)."""


class _LogDoCircuito(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        logger.warning("circuit_breaker=%s %s -> %s", cb.name, old_state.name, new_state.name)


def _novo_disjuntor(nome: str) -> pybreaker.CircuitBreaker:
    return pybreaker.CircuitBreaker(
        fail_max=CB_FALHAS_MAX,
        reset_timeout=CB_RESET_SEGUNDOS,
        name=nome,
        listeners=[_LogDoCircuito()],
    )


disjuntores = {
    "predict": _novo_disjuntor("inferencia/predict"),
    "similares": _novo_disjuntor("inferencia/similares"),
}


@retry(
    stop=stop_after_attempt(RETRY_TENTATIVAS),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(ErroTransitorio),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _requisitar_com_retentativa(metodo: str, url: str, **kwargs) -> requests.Response:
    try:
        resposta = requests.request(metodo, url, timeout=TIMEOUT_SEGUNDOS, **kwargs)
    except (requests.ConnectionError, requests.Timeout) as erro:
        raise ErroTransitorio(f"{type(erro).__name__}") from erro
    if resposta.status_code >= 500:
        raise ErroTransitorio(f"HTTP {resposta.status_code}")
    return resposta


def chamar(circuito: str, metodo: str, url: str, **kwargs) -> requests.Response:
    """Chamada HTTP protegida por circuit breaker + retentativa + tempo-limite.

    Levanta pybreaker.CircuitBreakerError se o circuito estiver aberto e
    ErroTransitorio se o servico continuar falhando apos as retentativas.
    """
    return disjuntores[circuito].call(_requisitar_com_retentativa, metodo, url, **kwargs)
