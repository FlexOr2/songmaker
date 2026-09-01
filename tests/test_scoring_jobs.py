"""The lyrical-coherence judge is provider-neutral, like the co-writer (#315).

Judge provider/model are their own settings, stored and validated like the
co-writer's pair but never coupled to it, and the scoring job resolves and
uses them instead of a Claude-only path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import TEST_SECRET, make_fake_redis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from songmaker_cli.api_models.whisper import WhisperCue
from songmaker_cli.app_context import AppContext
from songmaker_cli.constants import CLAUDE_SCORING_MODEL_DEFAULT, JUDGE_DEFAULT_PROVIDER
from songmaker_cli.db.engine import init_test_db as init_db
from songmaker_cli.db.models import Album, Generation, Job, Song, User, Version
from songmaker_cli.db.queries.settings import (
    get_cowriter_model,
    get_cowriter_provider,
    get_judge_model,
    get_judge_provider,
    set_claude_model,
    set_cowriter_settings,
    set_judge_settings,
)
from songmaker_cli.jobs.scoring import run_scoring_job
from songmaker_cli.middleware import AuthenticatedUser, get_current_user
from songmaker_cli.scoring.models import (
    EmotionalDynamicsScore,
    ScorerOutcome,
    ScorerRun,
    SongScores,
    TextAccuracyScore,
)

LIVE_CATALOG = {
    "claude": ["claude-opus-4-6", "claude-sonnet-4-6"],
    "grok": ["grok-4.6", "grok-4.5"],
    "codex": ["gpt-5.4"],
}


# ── settings layer ───────────────────────────────────────────────────


def test_judge_defaults_to_claude_and_todays_scoring_model(tmp_path: Path) -> None:
    """Defaults stay exactly what they were before #315: nothing tips over
    for a musician who never touches the new judge settings."""
    factory = init_db(tmp_path / "settings.db")
    with factory() as session:
        assert get_judge_provider(session) == JUDGE_DEFAULT_PROVIDER == "claude"
        assert get_judge_model(session, "claude") == CLAUDE_SCORING_MODEL_DEFAULT

        set_claude_model(session, "claude_scoring_model", "claude-haiku-4-5-20251001")
        session.commit()
        assert get_judge_model(session, "claude") == "claude-haiku-4-5-20251001"


def test_judge_settings_are_not_coupled_to_the_cowriters(tmp_path: Path) -> None:
    """Each task gets its own provider choice (operator ruling, #315)."""
    factory = init_db(tmp_path / "settings.db")
    with factory() as session:
        set_cowriter_settings(session, "codex", "gpt-5.4")
        set_judge_settings(session, "grok", "grok-4.6")
        session.commit()

        assert get_cowriter_provider(session) == "codex"
        assert get_cowriter_model(session, "codex") == "gpt-5.4"
        assert get_judge_provider(session) == "grok"
        assert get_judge_model(session, "grok") == "grok-4.6"


# ── /api/settings/judge ──────────────────────────────────────────────


def _fake_user(user_id: str, role: str = "admin"):
    user = AuthenticatedUser(id=user_id, username=f"u-{user_id}", role=role, is_active=True)
    return lambda: user


@pytest.fixture()
def admin_client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "songmaker_cli.cowriter.catalog.list_provider_models",
        lambda provider: list(LIVE_CATALOG[provider]),
    )
    factory = init_db(tmp_path / "judge_api.db")
    with factory() as session:
        session.add(User(id="u-test", username="user-u-test", password_hash="x", role="admin"))
        session.commit()
    ctx = AppContext(
        db=factory,
        audio_dir=tmp_path / "audio",
        data_dir=tmp_path / "data",
        session_secret=TEST_SECRET,
        redis=make_fake_redis(),
    )
    from songmaker_cli.api import router
    app = FastAPI()
    app.state.ctx = ctx
    app.dependency_overrides[get_current_user] = _fake_user("u-test")
    app.include_router(router)
    yield TestClient(app), factory


def test_unknown_judge_provider_rejected_at_the_settings_boundary(admin_client) -> None:
    client, _ = admin_client
    resp = client.put("/api/settings/judge", json={"provider": "bob", "model": "x"})
    assert resp.status_code == 422


def test_judge_model_must_be_in_the_live_catalog(admin_client) -> None:
    client, _ = admin_client
    rejected = client.put("/api/settings/judge", json={"provider": "grok", "model": "grok-4"})
    assert rejected.status_code == 422

    accepted = client.put("/api/settings/judge", json={"provider": "grok", "model": "grok-4.6"})
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["provider"] == "grok"
    assert body["model"] == "grok-4.6"

    fetched = client.get("/api/settings/judge").json()
    assert fetched["provider"] == "grok"
    assert fetched["model"] == "grok-4.6"


def test_judge_and_cowriter_settings_are_independent_through_the_api(admin_client) -> None:
    client, _ = admin_client
    client.put("/api/settings/cowriter", json={"provider": "codex", "model": "gpt-5.4"})
    client.put("/api/settings/judge", json={"provider": "grok", "model": "grok-4.6"})

    cowriter = client.get("/api/settings/cowriter").json()
    judge = client.get("/api/settings/judge").json()
    assert cowriter["provider"] == "codex"
    assert judge["provider"] == "grok"


# ── run_scoring_job wiring ───────────────────────────────────────────


def _seeded_generation(tmp_path: Path) -> tuple:
    factory = init_db(tmp_path / "scoring.db")
    with factory() as session:
        session.add(User(id="u1", username="user1", password_hash="x", role="user"))
        session.flush()
        session.add(Album(id="alb1", title="Rock", artist="Band", created_by="u1"))
        session.add(Song(id="s1", title="Song One", album_id="alb1", track_number=1))
        session.add(Version(
            id="v1", song_id="s1", version_number=1,
            lyrics="Hello world", prompt="rock style", bpm=120,
        ))
        session.add(Generation(
            id="g1", song_id="s1", version_id="v1", generation_number=1,
            mp3_path="user1/g1.mp3", seed=42,
        ))
        session.add(Job(id="j-score", type="score", status="queued"))
        session.commit()

    audio_dir = tmp_path / "audio"
    mp3 = audio_dir / "user1" / "g1.mp3"
    mp3.parent.mkdir(parents=True, exist_ok=True)
    mp3.write_bytes(b"fake-mp3")
    return factory, audio_dir


def _scoring_result_with_transcript() -> SongScores:
    return SongScores(
        emotional_dynamics=EmotionalDynamicsScore(
            pitch_cv=0.3, rms_contrast=2.0, onset_rate_cv=0.2,
            overall_expressiveness=0.55,
        ),
        text_accuracy=TextAccuracyScore(
            similarity_ratio=0.9,
            intended_line_texts=("hello", "world"),
            transcribed_line_texts=("hello", "world"),
            whisper_cues=(WhisperCue(start=0.0, end=0.8, text="hello world"),),
        ),
        runs=(
            ScorerRun(scorer="emotional_dynamics", outcome=ScorerOutcome.OK),
            ScorerRun(scorer="text_accuracy", outcome=ScorerOutcome.OK),
        ),
    )


def test_run_scoring_job_uses_the_configured_judge_provider_not_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A musician who points scoring at a different provider than the
    co-writer sees that provider judge the take (#315's done-when)."""
    factory, audio_dir = _seeded_generation(tmp_path)
    with factory() as session:
        set_judge_settings(session, "grok", "grok-4.6")
        session.commit()

    monkeypatch.setenv("XAI_API_KEY", "grok-key")
    from songmaker_cli.settings import get_settings
    get_settings.cache_clear()

    with (
        patch(
            "songmaker_cli.jobs.get_scorer_process",
            return_value=MagicMock(score=MagicMock(
                return_value=_scoring_result_with_transcript(),
            )),
        ),
        patch(
            "songmaker_cli.cowriter.dispatch.call_openai_compatible_once",
            return_value='{"score": 8, "issues": [], "summary": "grok verdict"}',
        ) as grok_call,
        patch("songmaker_cli.cowriter.claude_adapter.call_claude") as claude_call,
    ):
        run_scoring_job("j-score", "g1", None, db_factory=factory, audio_dir=audio_dir)

    assert grok_call.call_args.kwargs["provider"] == "grok"
    assert grok_call.call_args.kwargs["model"] == "grok-4.6"
    claude_call.assert_not_called()

    from songmaker_cli.db.models import Score
    with factory() as session:
        stored = session.query(Score).filter_by(generation_id="g1").one()
        assert stored.value["lyrical_coherence"] == 8


def test_run_scoring_job_fails_the_judge_loudly_when_its_provider_is_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured Grok credential must never fall back to Claude — the
    generation keeps no lyrical_coherence score and the reason is named."""
    factory, audio_dir = _seeded_generation(tmp_path)
    with factory() as session:
        set_judge_settings(session, "grok", "grok-4.6")
        session.commit()

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    from songmaker_cli.settings import get_settings
    get_settings.cache_clear()

    with (
        patch(
            "songmaker_cli.jobs.get_scorer_process",
            return_value=MagicMock(score=MagicMock(
                return_value=_scoring_result_with_transcript(),
            )),
        ),
        patch("songmaker_cli.cowriter.claude_adapter.call_claude") as claude_call,
    ):
        run_scoring_job("j-score", "g1", None, db_factory=factory, audio_dir=audio_dir)

    claude_call.assert_not_called()
    from songmaker_cli.db.models import Score
    with factory() as session:
        stored = session.query(Score).filter_by(generation_id="g1").one()
        assert "lyrical_coherence" not in stored.value
