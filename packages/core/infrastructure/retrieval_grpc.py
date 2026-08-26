"""gRPC client for the retrieval service, implementing the :class:`Retriever` protocol.

This is the "grpc" provider for the retrieval capability seam: it maps the SearchHit proto
messages back to the ``{id, text, score, meta}`` dicts the tools/agent already consume.

The client authenticates with a shared secret (``authorization: Bearer <token>``) and speaks
TLS when a CA bundle is configured, matching the server's :class:`~apps.retrieval.server.AuthGuard`.
Filters are stringified for the ``map<string, string>`` proto; a ``None`` ``user_id`` (guest)
becomes an explicit ``guest=1`` marker, since the wire format cannot carry a null.

The protobuf stubs are imported lazily so that the API still starts in ``in_process`` mode
without having run ``scripts/gen_proto.sh`` first.
"""
import json
from pathlib import Path

import grpc


class GrpcRetriever:
    def __init__(self, address: str, token: str = "", tls_ca: Path | None = None) -> None:
        from core.infrastructure.proto import retrieval_pb2, retrieval_pb2_grpc

        self._pb2 = retrieval_pb2
        if tls_ca is not None:
            with open(tls_ca, "rb") as fh:
                creds = grpc.ssl_channel_credentials(fh.read())
            self._channel = grpc.aio.secure_channel(address, creds)
        else:
            self._channel = grpc.aio.insecure_channel(address)
        self._stub = retrieval_pb2_grpc.RetrievalServiceStub(self._channel)
        self._token = token

    def _metadata(self) -> tuple:
        return (("authorization", f"Bearer {self._token}"),) if self._token else ()

    @staticmethod
    def _stringify_filters(filters: dict | None) -> dict:
        """Encode filters for the ``map<string, string>`` proto; ``None`` user → guest=1."""
        out: dict[str, str] = {}
        for key, value in (filters or {}).items():
            if value is None:
                if key == "user_id":
                    out["guest"] = "1"  # the proto cannot carry a null tenant scope
                continue
            out[key] = str(value)
        return out

    async def retrieve(self, query: str, top_k: int = 5, filters: dict | None = None) -> list[dict]:
        request = self._pb2.RetrieveRequest(
            query=query, top_k=top_k, filters=self._stringify_filters(filters)
        )
        response = await self._stub.Retrieve(request, metadata=self._metadata())
        return [
            {
                "id": h.id,
                "text": h.text,
                "score": h.score,
                "meta": json.loads(h.meta) if h.meta else None,
            }
            for h in response.hits
        ]

    async def health(self) -> str:
        response = await self._stub.Health(self._pb2.HealthRequest(), metadata=self._metadata())
        return response.status

    async def close(self) -> None:
        await self._channel.close()
