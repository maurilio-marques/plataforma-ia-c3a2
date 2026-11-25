FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Treina o modelo durante o build: o container ja sobe com o modelo pronto
RUN python -c "from modelo import carregar_modelo; carregar_modelo()"

RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

ENV HOST=0.0.0.0 PORT=8001
EXPOSE 8001

CMD uvicorn app:app --host ${HOST} --port ${PORT} --no-access-log
