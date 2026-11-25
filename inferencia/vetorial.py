"""
Cliente do armazenamento vetorial (Qdrant, via API REST).

Cada texto analisado vira um vetor de 256 numeros (embedding) e e guardado no
Qdrant junto com o sentimento. Depois, /similares acha os textos mais
parecidos pela distancia do cosseno.

O embedding usa HashingVectorizer do scikit-learn: deterministico, offline e
sem treino. A qualidade do modelo NAO pontua; o foco e o servico distribuido.

Retencao (LGPD): pontos com mais de VETOR_RETENCAO_DIAS sao apagados por uma
limpeza periodica (a cada hora).
"""
import logging
import os
import threading
import time
import uuid

import requests
from sklearn.feature_extraction.text import HashingVectorizer

logger = logging.getLogger("inferencia.vetorial")

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLECAO = os.getenv("QDRANT_COLECAO", "textos")
DIMENSAO = 256
TIMEOUT = float(os.getenv("QDRANT_TIMEOUT_SEGUNDOS", "2"))
RETENCAO_DIAS = int(os.getenv("VETOR_RETENCAO_DIAS", "7"))

_vetorizador = HashingVectorizer(
    n_features=DIMENSAO, analyzer="char_wb", ngram_range=(2, 4),
    alternate_sign=False, norm="l2",
)
_colecao_pronta = False
_trava = threading.Lock()


def embedding(texto: str) -> list[float]:
    return _vetorizador.transform([texto.lower()]).toarray()[0].tolist()


def _url(caminho: str) -> str:
    return f"{QDRANT_URL}/collections/{COLECAO}{caminho}"


def garantir_colecao() -> None:
    """Cria a colecao na primeira vez (idempotente)."""
    global _colecao_pronta
    if _colecao_pronta:
        return
    with _trava:
        if _colecao_pronta:
            return
        r = requests.get(_url(""), timeout=TIMEOUT)
        if r.status_code == 404:
            r = requests.put(_url(""), timeout=TIMEOUT,
                             json={"vectors": {"size": DIMENSAO, "distance": "Cosine"}})
            r.raise_for_status()
            logger.info("colecao %s criada no qdrant", COLECAO)
        else:
            r.raise_for_status()
        _colecao_pronta = True


def _requisitar(metodo: str, caminho: str, corpo: dict) -> requests.Response:
    """Faz a chamada ao Qdrant; se a colecao sumiu (ex.: Qdrant reiniciou sem
    volume), recria a colecao e tenta UMA vez de novo."""
    global _colecao_pronta
    garantir_colecao()
    r = requests.request(metodo, _url(caminho), json=corpo, timeout=TIMEOUT)
    if r.status_code == 404:
        _colecao_pronta = False
        garantir_colecao()
        r = requests.request(metodo, _url(caminho), json=corpo, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def salvar(texto: str, resultado: dict) -> None:
    # id derivado do texto: o mesmo texto nao gera pontos duplicados
    ponto_id = str(uuid.uuid5(uuid.NAMESPACE_URL, texto))
    ponto = {
        "id": ponto_id,
        "vector": embedding(texto),
        "payload": {
            "texto": texto,
            "sentimento": resultado["sentimento"],
            "confianca": resultado["confianca"],
            "criado_em": int(time.time()),
        },
    }
    _requisitar("PUT", "/points?wait=true", {"points": [ponto]})


def buscar(texto: str, limite: int) -> list[dict]:
    r = _requisitar("POST", "/points/search", {
        "vector": embedding(texto), "limit": limite, "with_payload": True,
    })
    return [
        {
            "texto": p["payload"].get("texto"),
            "sentimento": p["payload"].get("sentimento"),
            "similaridade": round(p["score"], 4),
        }
        for p in r.json()["result"]
    ]


def apagar_expirados() -> None:
    limite = int(time.time()) - RETENCAO_DIAS * 86400
    _requisitar("POST", "/points/delete?wait=true", {
        "filter": {"must": [{"key": "criado_em", "range": {"lt": limite}}]},
    })


def iniciar_limpeza_periodica(intervalo_segundos: int = 3600) -> None:
    def laco():
        while True:
            try:
                apagar_expirados()
                logger.info("limpeza de retencao executada (> %s dias)", RETENCAO_DIAS)
            except Exception as erro:  # noqa: BLE001
                logger.warning("limpeza de retencao falhou: %s", type(erro).__name__)
            time.sleep(intervalo_segundos)

    threading.Thread(target=laco, daemon=True, name="limpeza-vetorial").start()
