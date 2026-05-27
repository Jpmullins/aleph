"""Background jobs registered with the Arq worker."""

from aleph_workers.jobs.chunk_embed import chunk_embed_job
from aleph_workers.jobs.normalize import normalize_job
from aleph_workers.jobs.smoketest import smoke_llm_job
from aleph_workers.jobs.wiki_ingest import wiki_ingest_job

__all__ = [
    "chunk_embed_job",
    "normalize_job",
    "smoke_llm_job",
    "wiki_ingest_job",
]
