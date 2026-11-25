FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home appuser
USER appuser

# -u: logs sem buffer (aparecem na hora no docker compose logs)
CMD ["python", "-u", "worker.py"]
