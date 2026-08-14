"""gRPC service implementation: map proto requests onto the in-process RAG pipeline."""
import json

from core.infrastructure.proto import retrieval_pb2, retrieval_pb2_grpc


class RetrievalService(retrieval_pb2_grpc.RetrievalServiceServicer):
    def __init__(self, pipeline) -> None:
        self.pipeline = pipeline

    async def Retrieve(self, request, context):
        top_k = request.top_k if request.top_k > 0 else 5
        filters = dict(request.filters) if request.filters else None
        hits = await self.pipeline.retrieve(request.query, top_k, filters)
        return retrieval_pb2.RetrieveResponse(
            hits=[
                retrieval_pb2.SearchHit(
                    id=h["id"],
                    text=h["text"],
                    score=h["score"],
                    meta=json.dumps(h.get("meta"), ensure_ascii=False) if h.get("meta") else "",
                )
                for h in hits
            ]
        )

    async def Health(self, request, context):
        return retrieval_pb2.HealthResponse(status="ok")
