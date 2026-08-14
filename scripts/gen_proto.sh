#!/usr/bin/env bash
# Generate the Python protobuf + gRPC stubs for the retrieval service.
#
# Prefers `buf generate` when the buf CLI is available (for reproducible, linted output);
# otherwise falls back to grpc_tools.protoc, which is already a Python dependency.
#
# Output lands in packages/shared/proto/retrieval/v1/ and is imported as
# `retrieval.v1.retrieval_pb2` / `retrieval.v1.retrieval_pb2_grpc`.
set -euo pipefail

cd "$(dirname "$0")/.."

OUT="packages/shared/proto"
PROTO="proto/retrieval/v1/retrieval.proto"

mkdir -p "$OUT"

if command -v buf >/dev/null 2>&1; then
  buf generate
else
  python -m grpc_tools.protoc \
    -I proto \
    --python_out="$OUT" \
    --grpc_python_out="$OUT" \
    "$PROTO"
fi

# Make the generated directories importable packages (retrieval.v1.*).
touch "$OUT/retrieval/__init__.py"
touch "$OUT/retrieval/v1/__init__.py"

echo "Generated protobuf/gRPC stubs into $OUT/retrieval/v1/"
