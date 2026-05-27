"""Agent-token mint + verify round-trip tests."""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from aleph_core.errors import PermissionDenied
from aleph_security.agent_token import mint_agent_token, verify_agent_token


def test_mint_verify_roundtrip() -> None:
    secret = "x" * 64
    user_id = uuid4()
    project_id = uuid4()
    agent_run_id = uuid4()
    token = mint_agent_token(
        secret=secret,
        user_id=user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id="abc",
        ttl_seconds=60,
    )
    claims = verify_agent_token(token, secret=secret)
    assert claims.user_id == user_id
    assert claims.project_id == project_id
    assert claims.agent_run_id == agent_run_id
    assert claims.actor_kind == "aleph_agent"
    assert claims.correlation_id == "abc"
    assert claims.exp > int(time.time())


def test_verify_wrong_secret() -> None:
    secret = "x" * 64
    token = mint_agent_token(
        secret=secret,
        user_id=uuid4(),
        project_id=uuid4(),
        agent_run_id=uuid4(),
        actor_kind="aleph_agent",
        correlation_id="c",
    )
    with pytest.raises(PermissionDenied):
        verify_agent_token(token, secret="y" * 64)


def test_ttl_out_of_range() -> None:
    secret = "x" * 64
    with pytest.raises(ValueError):
        mint_agent_token(
            secret=secret,
            user_id=uuid4(),
            project_id=uuid4(),
            agent_run_id=uuid4(),
            actor_kind="aleph_agent",
            correlation_id="c",
            ttl_seconds=0,
        )
    with pytest.raises(ValueError):
        mint_agent_token(
            secret=secret,
            user_id=uuid4(),
            project_id=uuid4(),
            agent_run_id=uuid4(),
            actor_kind="aleph_agent",
            correlation_id="c",
            ttl_seconds=99_999,
        )
