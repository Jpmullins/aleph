"""Background jobs registered with the Arq worker."""

from aleph_workers.jobs.bootstrap import bootstrap_project_job
from aleph_workers.jobs.builder import builder_job
from aleph_workers.jobs.chunk_embed import chunk_embed_job
from aleph_workers.jobs.curate import curate_page_job
from aleph_workers.jobs.normalize import normalize_job
from aleph_workers.jobs.reembed import reembed_job
from aleph_workers.jobs.render_code import render_code_artifact_job
from aleph_workers.jobs.research import deep_research_job
from aleph_workers.jobs.reviewers import (
    editorial_review_job,
    mechanical_review_job,
)
from aleph_workers.jobs.smoketest import smoke_llm_job
from aleph_workers.jobs.wiki_ingest import wiki_ingest_job
from aleph_workers.jobs.wiki_refresh import refresh_stale_pages_job, wiki_refresh_job

__all__ = [
    "bootstrap_project_job",
    "builder_job",
    "chunk_embed_job",
    "curate_page_job",
    "deep_research_job",
    "editorial_review_job",
    "mechanical_review_job",
    "normalize_job",
    "reembed_job",
    "refresh_stale_pages_job",
    "render_code_artifact_job",
    "smoke_llm_job",
    "wiki_ingest_job",
    "wiki_refresh_job",
]
