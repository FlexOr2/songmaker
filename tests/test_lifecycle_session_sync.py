"""Recovery contracts for the lifecycle-owned Redis session reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from conftest import TEST_SECRET, make_fake_redis

from songmaker_cli.app_context import AppContext
from songmaker_cli.db.engine import init_test_db
from songmaker_cli.db.models import User, UserSession
from songmaker_cli.lifecycle import _sync_sessions


class _FixedClock:
    now_value = datetime(2030, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz: timezone) -> datetime:
        assert tz is timezone.utc
        return cls.now_value


@pytest.fixture
def ctx(tmp_path):
    factory = init_test_db(tmp_path / "songmaker.db")
    return AppContext(
        db=factory,
        audio_dir=tmp_path / "audio",
        data_dir=tmp_path / "data",
        session_secret=TEST_SECRET,
        redis=make_fake_redis(),
    )


def test_session_sync_updates_live_sessions_and_evicts_stale_cache_entries(
    ctx: AppContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _FixedClock.now_value
    with ctx.db() as session:
        session.add_all((
            User(id="active", username="active", password_hash="x"),
            User(id="inactive", username="inactive", password_hash="x", is_active=False),
            UserSession(id="live", user_id="active", expires_at=now + timedelta(days=1)),
            UserSession(
                id="inactive-session",
                user_id="inactive",
                expires_at=now + timedelta(days=1),
            ),
            UserSession(
                id="expired",
                user_id="active",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            ),
        ))
        session.commit()

    session_cache = MagicMock()
    session_cache.get_all_sessions.return_value = [
        ("live", 300),
        ("inactive-session", 200),
        ("missing", 100),
    ]
    session_cache.get.return_value = SimpleNamespace(user_id="active")
    monkeypatch.setattr("songmaker_cli.lifecycle.datetime", _FixedClock)

    synced = _sync_sessions(ctx, session_cache)

    assert synced == 1
    session_cache.delete.assert_called_once_with("missing", "active")
    session_cache.delete_user_sessions.assert_called_once_with("inactive")
    with ctx.db() as session:
        live = session.get(UserSession, "live")
        assert live is not None
        assert live.expires_at.replace(tzinfo=timezone.utc) == now + timedelta(seconds=300)
        assert session.get(UserSession, "inactive-session") is not None
        assert session.get(UserSession, "expired") is None


def test_session_sync_purges_database_expiry_when_redis_has_no_sessions(
    ctx: AppContext,
) -> None:
    with ctx.db() as session:
        session.add(User(id="u1", username="u1", password_hash="x"))
        session.add(
            UserSession(
                id="expired",
                user_id="u1",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            ),
        )
        session.commit()

    session_cache = MagicMock()
    session_cache.get_all_sessions.return_value = []

    assert _sync_sessions(ctx, session_cache) == 0
    with ctx.db() as session:
        assert session.get(UserSession, "expired") is None
