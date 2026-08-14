# Retrieval service: the RAG pipeline (embedding + recall + rerank + rewrite) exposed over gRPC.
#
# Model inference (embedding / rerank / LLM) stays in the separate model containers; this
# service only orchestrates the retrieval DAG and owns pgvector/FTS access.
FROM python:3.12-slim

WORKDIR /app

# Install the project (base deps already include grpcio/grpcio-tools). The `rag` extra pulls
# sentence-transformers for the cross-encoder reranker; omit it if reranker_model stays empty.
COPY pyproject.toml README.md ./
COPY packages ./packages
COPY apps ./apps
COPY proto ./proto
COPY scripts ./scripts

RUN pip install --no-cache-dir ".[rag]"

# Generate the protobuf/gRPC stubs (falls back to grpc_tools.protoc when buf is absent).
RUN bash scripts/gen_proto.sh

ENV PYTHONPATH="/app/packages/core:/app/packages/shared/proto"

EXPOSE 50051

CMD ["python", "-m", "apps.retrieval.main"]
