FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Nao roda como root dentro do container
RUN useradd --create-home appuser
USER appuser

ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000

# PORT vem da nuvem (Railway/Render definem automaticamente); local usa 8000
CMD uvicorn app:app --host ${HOST} --port ${PORT} --no-access-log --proxy-headers --forwarded-allow-ips="*"
