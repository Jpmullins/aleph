"""Background jobs registered with the Arq worker."""

from aleph_workers.jobs.aiq_synthesis import aiq_synthesis_poll_job
from aleph_workers.jobs.assistant_turn import assistant_turn_job
from aleph_workers.jobs.builder import builder_job
from aleph_workers.jobs.chunk_embed import chunk_embed_job
from aleph_workers.jobs.normalize import normalize_job
from aleph_workers.jobs.reviewers import (
    editorial_review_job,
    mechanical_review_job,
)
from aleph_workers.jobs.smoketest import smoke_llm_job
from aleph_workers.jobs.wiki_ingest import wiki_ingest_job

__all__ = [
    "aiq_synthesis_poll_job",
    "assistant_turn_job",
    "builder_job",
    "chunk_embed_job",
    "editorial_review_job",
    "mechanical_review_job",
    "normalize_job",
    "smoke_llm_job",
    "wiki_ingest_job",
]
