"""
Seguranca do gateway: autenticacao por JWT e limite de requisicoes.

Fluxo:
  1. POST /login com usuario e senha -> o gateway devolve um token JWT
     assinado (HS256) que expira em JWT_EXPIRA_MINUTOS.
  2. As demais rotas exigem o cabecalho  Authorization: Bearer <token>.
     Sem token, token invalido ou expirado -> 401.
  3. Cada usuario pode fazer no maximo RATE_LIMIT_POR_MINUTO requisicoes por
     minuto (contador no Redis, entao vale mesmo com varias copias do gateway).
     Passou do limite -> 429.
"""
import hmac
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("gateway.seguranca")

JWT_SEGREDO = os.getenv("JWT_SEGREDO", "troque-este-segredo")
JWT_ALGORITMO = "HS256"
JWT_EXPIRA_MINUTOS = int(os.getenv("JWT_EXPIRA_MINUTOS", "30"))
USUARIO_DEMO = os.getenv("USUARIO_DEMO", "aluno")
SENHA_DEMO = os.getenv("SENHA_DEMO", "senha-demo")
RATE_LIMIT_POR_MINUTO = int(os.getenv("RATE_LIMIT_POR_MINUTO", "30"))

_bearer = HTTPBearer(auto_error=False)


def credenciais_validas(usuario: str, senha: str) -> bool:
    # compare_digest evita ataque de tempo (timing attack)
    return hmac.compare_digest(usuario, USUARIO_DEMO) and hmac.compare_digest(senha, SENHA_DEMO)


def emitir_token(usuario: str) -> str:
    agora = datetime.now(timezone.utc)
    payload = {"sub": usuario, "iat": agora, "exp": agora + timedelta(minutes=JWT_EXPIRA_MINUTOS)}
    return jwt.encode(payload, JWT_SEGREDO, algorithm=JWT_ALGORITMO)


def _nao_autorizado(motivo: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=motivo,
        headers={"WWW-Authenticate": "Bearer"},
    )


def usuario_autenticado(
    credenciais: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Dependencia do FastAPI: devolve o usuario do token ou responde 401."""
    if credenciais is None:
        raise _nao_autorizado("token ausente")
    try:
        payload = jwt.decode(credenciais.credentials, JWT_SEGREDO, algorithms=[JWT_ALGORITMO])
    except jwt.ExpiredSignatureError:
        raise _nao_autorizado("token expirado")
    except jwt.InvalidTokenError:
        raise _nao_autorizado("token invalido")
    return payload["sub"]


def aplicar_rate_limit(redis_cliente, chave_cliente: str) -> None:
    """Janela fixa de 60s por cliente. Responde 429 ao passar do limite."""
    janela = int(time.time() // 60)
    chave = f"ratelimit:{chave_cliente}:{janela}"
    try:
        contagem = redis_cliente.incr(chave)
        if contagem == 1:
            redis_cliente.expire(chave, 60)
    except Exception:  # noqa: BLE001
        # Se o Redis cair, preferimos nao derrubar o gateway inteiro (fail-open).
        logger.warning("rate_limit indisponivel (redis fora do ar); requisicao liberada")
        return
    if contagem > RATE_LIMIT_POR_MINUTO:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="limite de requisicoes por minuto excedido",
            headers={"Retry-After": str(60 - int(time.time()) % 60)},
        )
