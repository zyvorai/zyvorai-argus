# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from langchain_openai import OpenAIEmbeddings
from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode
from qdrant_client import QdrantClient, models

from knowledge.config import get_settings


@lru_cache(maxsize=1)
def get_embeddings() -> Any:
    """Dense embeddings: OpenAI-compatible by default, or local FastEmbed.

    Set EMBEDDING_BACKEND=fastembed for air-gapped / Ollama labs that lack an
    OpenAI-compatible embeddings endpoint. Uses the already-shipped `fastembed`
    extra (no langchain-community required).
    """
    settings = get_settings()
    backend = (os.environ.get("EMBEDDING_BACKEND") or "").strip().lower()
    model = settings.embedding_model or ""
    use_fast = backend in {"fastembed", "local"} or model.startswith("BAAI/") or model.startswith(
        "fastembed:"
    )
    if use_fast:
        from langchain_core.embeddings import Embeddings

        name = model.removeprefix("fastembed:") if model else "BAAI/bge-small-en-v1.5"
        if name in {"nomic-embed-text", "text-embedding-3-small", ""}:
            name = "BAAI/bge-small-en-v1.5"

        class _FastEmbedDense(Embeddings):
            def __init__(self, model_name: str) -> None:
                from fastembed import TextEmbedding

                self._model = TextEmbedding(model_name=model_name)

            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                return [list(v) for v in self._model.embed(texts)]

            def embed_query(self, text: str) -> list[float]:
                return [list(v) for v in self._model.query_embed(text)][0]

        return _FastEmbedDense(name)

    kwargs: dict[str, Any] = {
        "model": settings.embedding_model,
        "api_key": settings.resolved_embedding_key(),
    }
    if settings.embedding_base_url:
        kwargs["base_url"] = settings.embedding_base_url
    if settings.embedding_dimensions:
        kwargs["dimensions"] = settings.embedding_dimensions
    return OpenAIEmbeddings(**kwargs)


@lru_cache(maxsize=1)
def get_sparse_embeddings() -> FastEmbedSparse:
    return FastEmbedSparse(model_name="Qdrant/bm25")


def _client_options() -> dict[str, Any]:
    settings = get_settings()
    options: dict[str, Any] = {
        "url": settings.qdrant_url,
        "timeout": settings.qdrant_timeout_seconds,
    }
    if settings.qdrant_api_key:
        options["api_key"] = settings.qdrant_api_key.get_secret_value()
    return options


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    return QdrantClient(**_client_options())


@lru_cache(maxsize=1)
def get_vector_store() -> QdrantVectorStore:
    settings = get_settings()
    return QdrantVectorStore.construct_instance(
        embedding=get_embeddings(),
        sparse_embedding=get_sparse_embeddings(),
        retrieval_mode=RetrievalMode.HYBRID,
        client_options=_client_options(),
        collection_name=settings.qdrant_collection,
        sparse_vector_params={"modifier": models.Modifier.IDF},
    )


def qdrant_filter(
    *,
    tenant_id: str,
    access_levels: tuple[str, ...],
    product: str | None,
    document_type: str | None,
) -> models.Filter:
    settings = get_settings()

    tenant_conditions: list[models.Condition] = [
        models.FieldCondition(
            key="metadata.tenant_id",
            match=models.MatchValue(value=tenant_id),
        )
    ]
    if settings.enable_public_documents and tenant_id != "public":
        tenant_conditions.append(
            models.FieldCondition(
                key="metadata.tenant_id",
                match=models.MatchValue(value="public"),
            )
        )

    must: list[models.Condition] = [
        models.FieldCondition(
            key="metadata.access_level",
            match=models.MatchAny(any=list(access_levels)),
        )
    ]
    if product:
        must.append(
            models.FieldCondition(
                key="metadata.product_key",
                match=models.MatchValue(value=product.lower()),
            )
        )
    if document_type:
        must.append(
            models.FieldCondition(
                key="metadata.source",
                match=models.MatchValue(value=document_type),
            )
        )

    return models.Filter(must=must, should=tenant_conditions)



def delete_existing_sources(documents: list[Any]) -> None:
    """Remove old chunks for the same tenant/source paths before re-indexing."""
    if not documents:
        return

    grouped: dict[str, set[str]] = {}
    for document in documents:
        tenant_id = str(document.metadata.get("tenant_id") or "public")
        source_path = str(document.metadata.get("source_path") or "")
        if source_path:
            grouped.setdefault(tenant_id, set()).add(source_path)

    store = get_vector_store()
    for tenant_id, source_paths in grouped.items():
        store.client.delete(
            collection_name=store.collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="metadata.tenant_id",
                            match=models.MatchValue(value=tenant_id),
                        ),
                        models.FieldCondition(
                            key="metadata.source_path",
                            match=models.MatchAny(any=sorted(source_paths)),
                        ),
                    ]
                )
            ),
            wait=True,
        )
