"""
Acesso a fila Redis (reaproveitado do C1.A2, com retencao e trace).

Chaves no Redis:
  tarefas            lista FIFO de tarefas pendentes (o gateway faz RPUSH)
  dead_letter        tarefas que falharam MAX_TENTATIVAS vezes
  resultado:<id>     resultado/estado da tarefa (expira: RETENCAO_RESULTADO_SEGUNDOS)
  tentativa:<id>     contador de tentativas da tarefa
"""
import json
import os

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
FILA_TAREFAS = os.getenv("FILA_TAREFAS", "tarefas")
FILA_DEAD_LETTER = "dead_letter"
RETENCAO_RESULTADO_SEGUNDOS = int(os.getenv("RETENCAO_RESULTADO_SEGUNDOS", "86400"))

_cliente = None


def cliente():
    global _cliente
    if _cliente is None:
        _cliente = redis.from_url(REDIS_URL, decode_responses=True)
    return _cliente


def proxima_tarefa(timeout: int = 5):
    """Bloqueia ate chegar tarefa (ou timeout)."""
    item = cliente().blpop(FILA_TAREFAS, timeout=timeout)
    return json.loads(item[1]) if item else None


def reenfileirar(mensagem: dict) -> None:
    cliente().rpush(FILA_TAREFAS, json.dumps(mensagem))


def incrementar_tentativa(tarefa_id: str) -> int:
    chave = f"tentativa:{tarefa_id}"
    n = int(cliente().incr(chave))
    cliente().expire(chave, RETENCAO_RESULTADO_SEGUNDOS)
    return n


def guardar_resultado(tarefa_id: str, resultado: dict) -> None:
    cliente().set(f"resultado:{tarefa_id}", json.dumps(resultado), ex=RETENCAO_RESULTADO_SEGUNDOS)


def guardar_dead_letter(tarefa_id: str, erro: str, tentativas: int) -> None:
    # O texto do usuario NAO vai para a dead-letter (minimizacao de dados - LGPD)
    cliente().rpush(FILA_DEAD_LETTER, json.dumps(
        {"task_id": tarefa_id, "erro": erro, "tentativas": tentativas}))
    cliente().ltrim(FILA_DEAD_LETTER, -1000, -1)  # guarda no maximo as 1000 ultimas
