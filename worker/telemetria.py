"""
Rastreamento distribuido (OpenTelemetry) do worker.

Cada servico tem o seu proprio arquivo de telemetria: microsservicos nao
compartilham codigo, so contratos (HTTP e mensagens na fila).

Os spans sao enviados via OTLP/HTTP para o endereco em
OTEL_EXPORTER_OTLP_ENDPOINT (no docker-compose, o Jaeger). Sem essa variavel,
o rastreamento continua ativo (o trace_id e propagado), mas nada e exportado.
"""
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


def configurar(nome_servico: str):
    provedor = TracerProvider(resource=Resource.create({"service.name": nome_servico}))

    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provedor.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    if os.getenv("OTEL_CONSOLE", "false").lower() == "true":
        provedor.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provedor)
    return trace.get_tracer(nome_servico)
