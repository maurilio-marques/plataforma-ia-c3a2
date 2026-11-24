# Deploy no Railway (free tier) — passo a passo

O Railway roda cada serviço do `docker-compose.yml` como um serviço separado,
numa rede privada (`*.railway.internal`). Só o **gateway** recebe domínio público,
com **HTTPS automático** (certificado do próprio Railway).

> Plano: contas novas começam no *Trial* (US$ 5 de crédito único, 30 dias, sem cartão).
> Acompanhe o consumo em **Usage**. Depois do Trial a conta vira *Free* (US$ 1/mês).

## 0. Antes de começar
- O repositório já precisa estar no GitHub com todos os commits.
- Gere um segredo forte para o JWT (rode no seu computador):
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```

## 1. Criar o projeto
1. Acesse https://railway.com e entre com **Login with GitHub**.
2. **New Project → Empty Project**.

## 2. Redis (fila + resultados)
1. No projeto: **+ Create → Database → Redis**.
2. Deixe o nome como `Redis`. Ele cria a variável `REDIS_URL` sozinho.

## 3. Qdrant (armazenamento vetorial)
1. **+ Create → Docker Image** → digite `qdrant/qdrant:v1.12.4`.
2. Em **Settings**, renomeie o serviço para `qdrant`.
3. Em **Variables**, adicione:
   | Variável | Valor |
   |---|---|
   | `QDRANT__SERVICE__HOST` | `::` |

## 4. Serviço de inferência
1. **+ Create → GitHub Repo** → escolha o repositório do grupo.
2. **Settings**: renomeie para `inferencia` e, em **Source → Root Directory**, coloque `/inferencia`.
3. **Variables**:
   | Variável | Valor |
   |---|---|
   | `HOST` | `::` |
   | `PORT` | `8001` |
   | `QDRANT_URL` | `http://qdrant.railway.internal:6333` |

## 5. Worker
1. **+ Create → GitHub Repo** → mesmo repositório.
2. **Settings**: renomeie para `worker`, **Root Directory** = `/worker`.
3. **Variables**:
   | Variável | Valor |
   |---|---|
   | `REDIS_URL` | `${{Redis.REDIS_URL}}` |
   | `URL_INFERENCIA` | `http://inferencia.railway.internal:8001` |

## 6. Gateway (único serviço público)
1. **+ Create → GitHub Repo** → mesmo repositório.
2. **Settings**: renomeie para `gateway`, **Root Directory** = `/gateway`.
3. **Variables**:
   | Variável | Valor |
   |---|---|
   | `HOST` | `::` |
   | `PORT` | `8000` |
   | `REDIS_URL` | `${{Redis.REDIS_URL}}` |
   | `URL_INFERENCIA` | `http://inferencia.railway.internal:8001` |
   | `JWT_SEGREDO` | *(o segredo gerado no passo 0)* |
   | `USUARIO_DEMO` | `aluno` |
   | `SENHA_DEMO` | *(uma senha forte; informe ao professor no README ou no AVA)* |
4. **Settings → Networking → Generate Domain** (porta `8000`).
   Sai algo como `https://plataforma-ia-xxxx.up.railway.app`. **Essa é a URL pública.**

## 7. Clicar em Deploy e testar
Clique em **Deploy** (canto superior) para aplicar tudo. Quando os 5 serviços ficarem verdes:
```bash
URL=https://SEU-DOMINIO.up.railway.app
curl $URL/saude
```
Ou abra `$URL/docs` no navegador e teste pelo Swagger (botão **Authorize** depois do `/login`).

Coloque a URL no `README.md` (seção **URL pública**) e faça commit.

## 8. (Opcional) Jaeger na nuvem
O rastreamento já é demonstrado localmente (`docker compose up` → http://localhost:16686).
Se quiserem os traces também na nuvem:
1. **+ Create → Docker Image** → `jaegertracing/all-in-one:1.62.0`, renomeie para `jaeger`,
   variável `COLLECTOR_OTLP_ENABLED=true`.
2. Em `gateway`, `worker` e `inferencia`, adicione
   `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger.railway.internal:4318`.
3. Para ver o painel, **Generate Domain** no `jaeger` (porta `16686`). O painel não tem senha:
   tirem o print e removam o domínio depois.

## Problemas comuns
| Sintoma | Causa provável |
|---|---|
| Gateway responde 503 "apos retentativas" | `inferencia` ainda subindo ou `URL_INFERENCIA` errada |
| Worker com "fila indisponivel" no log | `REDIS_URL` sem a referência `${{Redis.REDIS_URL}}` |
| `vetor_armazenado: false` | variável `QDRANT__SERVICE__HOST=::` faltando no `qdrant` |
| 502 no domínio público | porta do domínio diferente de `PORT` do gateway |
