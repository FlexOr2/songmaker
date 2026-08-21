"""Tests for ``scripts/lyric_alignment_spike.py``."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lyric_alignment_spike.py"


def _load_spike():
    spec = importlib.util.spec_from_file_location("lyric_alignment_spike", _SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


spike = _load_spike()


def _case(name: str):
    for case in spike.synthetic_corpus():
        if case.name == name:
            return case
    raise AssertionError(f"missing corpus case {name}")


def _mapped(alignment) -> set[int]:
    return {interval.line_index for interval in alignment.intervals}


def _align(case_name: str, candidate: str, **kwargs):
    case = _case(case_name)
    return spike.align(case.lyrics, case.cues, candidate, **kwargs)


class _FakeCursor:
    def __init__(self, counts: tuple[int, int, int, int], rows: list[tuple]) -> None:
        self.counts = counts
        self.rows = rows
        self.sqls: list[str] = []

    def execute(self, sql: str) -> None:
        self.sqls.append(sql)

    def fetchone(self) -> tuple[int, int, int, int]:
        return self.counts

    def fetchall(self) -> list[tuple]:
        return self.rows

    def close(self) -> None:
        return None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def test_defaults_are_named_spike_candidates_not_silent() -> None:
    assert spike.MIN_RATIO == 0.72
    assert spike.AMBIGUITY_MARGIN == 0.12
    assert spike.CANDIDATES == (
        spike.CANDIDATE_NO_HIGHLIGHT,
        spike.CANDIDATE_GREEDY_MONOTONE,
        spike.CANDIDATE_COVER_THEN_SPLIT,
    )


def test_normalize_text_nfkc_casefold_punctuation_and_infix_apostrophes() -> None:
    assert spike.normalize_text("Don't Stop!") == "don't stop"
    assert spike.normalize_text("  Hello,   WORLD. ") == "hello world"
    assert spike.normalize_text("ﬁre") == "fire"
    assert spike.normalize_text("It’s fine") == "it's fine"
    assert spike.normalize_text("'quoted'") == "quoted"
    assert spike.normalize_text("rockin'") == "rockin"
    assert spike.normalize_text("Straße") == "strasse"


def test_parse_lyric_lines_drops_empty_and_section_marker_lines() -> None:
    lines = spike.parse_lyric_lines(
        "[verse]\nhello there\n\n[chorus]\nonly this\n...\n",
    )
    assert [line.original_index for line in lines] == [1, 4]
    assert lines[0].normalized == "hello there"
    assert lines[1].normalized == "only this"


def test_align_rejects_unknown_candidate() -> None:
    with pytest.raises(ValueError, match="unknown candidate"):
        spike.align("hello", (), "magic")


@pytest.mark.parametrize(
    "candidate",
    [
        spike.CANDIDATE_NO_HIGHLIGHT,
        spike.CANDIDATE_GREEDY_MONOTONE,
        spike.CANDIDATE_COVER_THEN_SPLIT,
    ],
)
def test_no_highlight_cases_map_nothing(candidate: str) -> None:
    for name in (
        "missing_cues",
        "empty_lyrics",
        "empty_normalized_cues",
        "all_ratios_below_min",
        "ambiguous_pair",
    ):
        alignment = _align(name, candidate)
        assert alignment.intervals == ()
        assert _mapped(alignment) == set()


def test_no_highlight_candidate_never_maps() -> None:
    for case in spike.synthetic_corpus():
        alignment = spike.align(
            case.lyrics, case.cues, spike.CANDIDATE_NO_HIGHLIGHT,
        )
        assert alignment.intervals == ()


@pytest.mark.parametrize(
    "candidate",
    [spike.CANDIDATE_GREEDY_MONOTONE, spike.CANDIDATE_COVER_THEN_SPLIT],
)
@pytest.mark.parametrize(
    "name",
    [
        "exact_line_matches",
        "repeated_chorus",
        "omitted_lyric_line",
        "extra_adlib_cue",
        "section_markers_ignored",
        "divergent_whisper_text",
        "split_cues_same_line",
    ],
)
def test_matcher_candidates_have_no_false_positive_highlights(
    candidate: str, name: str,
) -> None:
    case = _case(name)
    alignment = spike.align(case.lyrics, case.cues, candidate)
    predicted = _mapped(alignment)
    assert predicted <= set(case.expected_mapped_original_indices)


def test_exact_line_matches_intervals_follow_cue_times() -> None:
    case = _case("exact_line_matches")
    for candidate in (
        spike.CANDIDATE_GREEDY_MONOTONE,
        spike.CANDIDATE_COVER_THEN_SPLIT,
    ):
        alignment = spike.align(case.lyrics, case.cues, candidate)
        intervals = {item.line_index: item for item in alignment.intervals}
        assert intervals[0].start == 0.0 and intervals[0].end == 1.2
        assert intervals[1].start == 1.2 and intervals[1].end == 2.5
        assert intervals[2].start == 2.5 and intervals[2].end == 3.8


def test_repeated_chorus_maps_first_unused_then_next_repeat() -> None:
    case = _case("repeated_chorus")
    alignment = spike.align(
        case.lyrics, case.cues, spike.CANDIDATE_GREEDY_MONOTONE,
    )
    assert [item.line_index for item in alignment.intervals] == [1, 2, 5, 6, 9, 12, 13]
    marker_indices = {
        index
        for index, raw in enumerate(case.lyrics.splitlines())
        if raw.strip().startswith("[") and raw.strip().endswith("]")
    }
    assert _mapped(alignment).isdisjoint(marker_indices)


def test_omitted_lyric_line_stays_unmapped() -> None:
    alignment = _align("omitted_lyric_line", spike.CANDIDATE_GREEDY_MONOTONE)
    assert _mapped(alignment) == {0, 2}


def test_extra_adlib_does_not_steal_a_later_line() -> None:
    alignment = _align("extra_adlib_cue", spike.CANDIDATE_GREEDY_MONOTONE)
    assert _mapped(alignment) == {0, 1, 2}


def test_divergent_whisper_text_does_not_highlight_that_line() -> None:
    alignment = _align("divergent_whisper_text", spike.CANDIDATE_GREEDY_MONOTONE)
    assert 1 not in _mapped(alignment)
    assert _mapped(alignment) == {0, 2}


def test_section_marker_original_indices_are_never_mapped() -> None:
    case = _case("section_markers_ignored")
    alignment = spike.align(
        case.lyrics, case.cues, spike.CANDIDATE_GREEDY_MONOTONE,
    )
    assert _mapped(alignment) == {1, 3}
    assert 0 not in _mapped(alignment)
    assert 2 not in _mapped(alignment)


def test_cover_then_split_merges_consecutive_cues_on_one_line() -> None:
    greedy = _align("split_cues_same_line", spike.CANDIDATE_GREEDY_MONOTONE)
    cover = _align("split_cues_same_line", spike.CANDIDATE_COVER_THEN_SPLIT)
    assert _mapped(greedy) == {0}
    assert _mapped(cover) == {0}
    assert greedy.intervals[0].start == 0.0
    assert greedy.intervals[0].end == 1.1
    assert cover.intervals[0].start == 0.0
    assert cover.intervals[0].end == 2.4


def test_cover_then_split_does_not_map_backwards() -> None:
    lyrics = (
        "the zinc lantern leans\n"
        "marble moths in the rafters\n"
        "we count the quiet bells\n"
    )
    cues = (
        spike.WhisperCue(0.0, 1.0, "we count the quiet bells"),
        spike.WhisperCue(1.0, 2.0, "the zinc lantern leans"),
    )
    alignment = spike.align(lyrics, cues, spike.CANDIDATE_COVER_THEN_SPLIT)
    assert 0 not in _mapped(alignment)


def test_min_ratio_override_drops_a_weak_match() -> None:
    lyrics = "the zinc lantern leans\nwe count the quiet bells\n"
    cues = (
        spike.WhisperCue(0.0, 1.0, "the zinc lantern"),
        spike.WhisperCue(1.0, 2.0, "we count the quiet bells"),
    )
    loose = spike.align(
        lyrics, cues, spike.CANDIDATE_GREEDY_MONOTONE, min_ratio=0.5,
    )
    strict = spike.align(
        lyrics, cues, spike.CANDIDATE_GREEDY_MONOTONE, min_ratio=0.99,
    )
    assert 0 in _mapped(loose)
    assert 0 not in _mapped(strict)
    assert 1 in _mapped(strict)


def test_ambiguity_margin_override_can_allow_or_skip() -> None:
    case = _case("ambiguous_pair")
    skipped = spike.align(
        case.lyrics,
        case.cues,
        spike.CANDIDATE_GREEDY_MONOTONE,
        ambiguity_margin=0.12,
    )
    allowed = spike.align(
        case.lyrics,
        case.cues,
        spike.CANDIDATE_GREEDY_MONOTONE,
        ambiguity_margin=0.01,
    )
    assert skipped.intervals == ()
    assert _mapped(allowed) == {0}


def test_synthetic_corpus_false_positives_are_zero() -> None:
    summary = spike.evaluate_synthetic_corpus()
    for candidate in spike.CANDIDATES:
        false_positives, _true_positives, _missed = summary.totals(candidate)
        assert false_positives == 0, candidate


def test_synthetic_cli_prints_metrics_without_corpus_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    phrases: list[str] = []
    for case in spike.synthetic_corpus():
        phrases.extend(
            line.strip()
            for line in case.lyrics.splitlines()
            if line.strip() and not line.strip().startswith("[")
        )
        if case.cues is not None:
            phrases.extend(cue.text for cue in case.cues)
    rc = spike.main(["synthetic"])
    captured = capsys.readouterr()
    assert rc == 0
    blob = captured.out + captured.err
    assert "false_positives=" in captured.out
    assert spike.CANDIDATE_GREEDY_MONOTONE in captured.out
    for phrase in phrases:
        if len(phrase) < 8:
            continue
        assert phrase not in blob


def test_corpus_hash_is_sha256_of_concatenated_fields() -> None:
    assert spike.corpus_hash("a", "b") == hashlib.sha256(b"ab").hexdigest()


def test_live_probe_missing_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = spike.main(["live-probe"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "DATABASE_URL" in captured.err
    assert captured.out == ""


def test_live_probe_rejects_non_postgres_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    rc = spike.main(["live-probe"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "PostgreSQL" in captured.err


def test_live_probe_reports_counts_and_hashes_without_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    whisper_text = "zeta-prime sung line"
    lyrics = "zeta-prime lyric line"
    generation_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    cursor = _FakeCursor((180, 33, 0, 0), [(generation_id, whisper_text, lyrics)])
    conn = _FakeConn(cursor)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://songmaker:x@localhost/songmaker",
    )
    monkeypatch.setattr(spike, "_connect_postgres", lambda _dsn: conn)

    rc = spike.main(["live-probe"])
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert rc == 0
    assert conn.closed is True
    assert "whisper_text=33" in captured.out
    assert "whisper_cues_nonnull=0" in captured.out
    assert "whisper_cues_nonempty=0" in captured.out
    assert spike.NO_WHISPER_CUES in captured.out
    assert generation_id in captured.out
    assert spike.corpus_hash(whisper_text, lyrics) in captured.out
    assert whisper_text not in blob
    assert lyrics not in blob
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in cursor.sqls)
    assert not any("INSERT" in sql.upper() for sql in cursor.sqls)


def test_live_probe_with_cues_still_hides_text_and_does_not_align(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    whisper_text = "hidden sung payload"
    lyrics = "hidden lyric payload"
    generation_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    cursor = _FakeCursor((10, 1, 1, 1), [(generation_id, whisper_text, lyrics)])
    monkeypatch.setenv("DATABASE_URL", "postgres://songmaker@localhost/songmaker")
    monkeypatch.setattr(spike, "_connect_postgres", lambda _dsn: _FakeConn(cursor))

    rc = spike.main(["live-probe"])
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert rc == 0
    assert spike.CUES_PRESENT_NO_ALIGN in captured.out
    assert whisper_text not in blob
    assert lyrics not in blob
    assert "false_positives=" not in captured.out


def test_live_probe_sqlalchemy_dsn_is_converted_for_psycopg2() -> None:
    converted = spike.postgres_dsn(
        "postgresql+psycopg2://songmaker:x@localhost/songmaker",
    )
    assert converted.startswith("postgresql://")
    assert "+psycopg2" not in converted


def test_main_synthetic_uses_named_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = spike.main(["synthetic"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "min_ratio=0.72" in out
    assert "ambiguity_margin=0.12" in out
