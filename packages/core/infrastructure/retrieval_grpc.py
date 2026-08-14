"""gRPC client for the retrieval service, implementing the :class:`Retriever` protocol.

This is the "grpc" provider for the retrieval capability seam: it maps the SearchHit proto
messages back to the ``{id, text, score, meta}`` dicts the tools/agent already consume.

The protobuf stubs are imported lazily so that the API still starts in ``in_process`` mode
without having run ``scripts/gen_proto.sh`` first.
"""
import json

import grpc


class GrpcRetriever:
    def __init__(self, address: str) -> None:
        from core.infrastructure.proto import retrieval_pb2, retrieval_pb2_grpc

        self._pb2 = retrieval_pb2
        self._channel = grpc.aio.insecure_channel(address)
        self._stub = retrieval_pb2_grpc.RetrievalServiceStub(self._channel)

    async def retrieve(self, query: str, top_k: int = 5, filters: dict | None = None) -> list[dict]:
        request = self._pb2.RetrieveRequest(query=query, top_k=top_k, filters=filters or {})
        response = await self._stub.Retrieve(request)
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
        response = await self._stub.Health(self._pb2.HealthRequest())
        return response.status

    async def close(self) -> None:
        await self._channel.close()
