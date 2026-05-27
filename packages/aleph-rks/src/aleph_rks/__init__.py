"""Raw Knowledge Store: sources, assets, normalization, chunking, embedding.

Inc 1 introduces RKS. The wiki layer (`aleph_wiki`) consumes
`NormalizedDocument` rows and writes `WikiPage`/`SourcePage`. Embedding is
intra-source only — wiki-first retrieval routes through the wiki index,
not these chunks.
"""

from aleph_rks.chunking import Chunk, chunk_markdown
from aleph_rks.models import (
    Connector,
    ConnectorBinding,
    DocumentChunk,
    NormalizedDocument,
    RetrievalIndexRecord,
    Source,
    SourceAsset,
    SourceVersion,
)

__all__ = [
    "Chunk",
    "Connector",
    "ConnectorBinding",
    "DocumentChunk",
    "NormalizedDocument",
    "RetrievalIndexRecord",
    "Source",
    "SourceAsset",
    "SourceVersion",
    "chunk_markdown",
]
