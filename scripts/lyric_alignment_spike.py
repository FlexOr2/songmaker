"""Deterministic lyric↔Whisper alignment spike (issue #52).

NOT a production feature. No LLM, no player UI, no schema changes, no scoring.
MIN_RATIO and AMBIGUITY_MARGIN are spike candidates, not product defaults.

Usage:
  python scripts/lyric_alignment_spike.py synthetic
  python scripts/lyric_alignment_spike.py live-probe
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final

MIN_RATIO: Final = 0.72
AMBIGUITY_MARGIN: Final = 0.12

CANDIDATE_NO_HIGHLIGHT: Final = "no_highlight"
CANDIDATE_GREEDY_MONOTONE: Final = "greedy_monotone"
CANDIDATE_COVER_THEN_SPLIT: Final = "cover_then_split"
CANDIDATES: Final[tuple[str, ...]] = (
    CANDIDATE_NO_HIGHLIGHT,
    CANDIDATE_GREEDY_MONOTONE,
    CANDIDATE_COVER_THEN_SPLIT,
)

_SECTION_MARKER: Final = re.compile(r"^\[([^\[\]]+)\]$")
_INFIX_APOSTROPHE: Final = re.compile(r"(?<!\w)'|'(?!\w)")
_WHITESPACE: Final = re.compile(r"\s+")
_APOSTROPHE_TABLE: Final = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201b": "'",
    "\u02bc": "'",
})

_POSTGRES_PREFIXES: Final = (
    "postgres://",
    "postgresql://",
    "postgresql+psycopg2://",
)
_SQLALCHEMY_PSYCOPG2_PREFIX: Final = "postgresql+psycopg2://"
_PSYCOPG2_PREFIX: Final = "postgresql://"

DATABASE_URL_MISSING: Final = "DATABASE_URL is missing; live-probe requires a PostgreSQL URL"
DATABASE_URL_NOT_POSTGRES: Final = "DATABASE_URL must be a PostgreSQL URL"
NO_WHISPER_CUES: Final = "no whisper_cues present; not aligning or rescoring live takes"
CUES_PRESENT_NO_ALIGN: Final = "whisper_cues present; not aligning or rescoring live takes"

_LIVE_COUNTS_SQL: Final = """\
SELECT
    COUNT(*)::int,
    COUNT(*) FILTER (
        WHERE whisper_text IS NOT NULL AND btrim(whisper_text) <> ''
    )::int,
    COUNT(*) FILTER (WHERE whisper_cues IS NOT NULL)::int,
    COUNT(*) FILTER (
        WHERE whisper_cues IS NOT NULL
          AND jsonb_typeof(CAST(whisper_cues AS jsonb)) = 'array'
          AND jsonb_array_length(CAST(whisper_cues AS jsonb)) > 0
    )::int
FROM generations
"""

_LIVE_HASH_SQL: Final = """\
SELECT g.id, g.whisper_text, COALESCE(v.lyrics, '')
FROM generations AS g
LEFT JOIN versions AS v ON v.id = g.version_id
WHERE g.whisper_text IS NOT NULL AND btrim(g.whisper_text) <> ''
ORDER BY g.id
"""


@dataclass(frozen=True, slots=True)
class WhisperCue:
    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("end must not be before start")


@dataclass(frozen=True, slots=True)
class LyricLine:
    original_index: int
    normalized: str

    def __repr__(self) -> str:
        return (
            f"LyricLine(original_index={self.original_index}, "
            f"normalized_len={len(self.normalized)})"
        )


@dataclass(frozen=True, slots=True)
class LineInterval:
    line_index: int
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class Alignment:
    candidate: str
    intervals: tuple[LineInterval, ...]


@dataclass(frozen=True, slots=True)
class CorpusCase:
    name: str
    lyrics: str
    cues: tuple[WhisperCue, ...] | None
    expected_mapped_original_indices: frozenset[int]

    def __repr__(self) -> str:
        return f"CorpusCase(name={self.name!r})"


@dataclass(frozen=True, slots=True)
class CaseScore:
    case_name: str
    candidate: str
    false_positive_count: int
    true_positive_count: int
    missed_count: int
    expected_mapped_count: int


@dataclass(frozen=True, slots=True)
class EvalSummary:
    min_ratio: float
    ambiguity_margin: float
    scores: tuple[CaseScore, ...]

    def totals(self, candidate: str) -> tuple[int, int, int]:
        if candidate not in CANDIDATES:
            raise ValueError(f"unknown candidate: {candidate}")
        false_positives = 0
        true_positives = 0
        missed = 0
        for score in self.scores:
            if score.candidate != candidate:
                continue
            false_positives += score.false_positive_count
            true_positives += score.true_positive_count
            missed += score.missed_count
        return false_positives, true_positives, missed


@dataclass(frozen=True, slots=True)
class _PreparedCue:
    start: float
    end: float
    normalized: str


def normalize_text(text: str) -> str:
    mapped = unicodedata.normalize("NFKC", text).translate(_APOSTROPHE_TABLE).casefold()
    kept: list[str] = []
    for char in mapped:
        if char == "'":
            kept.append(char)
            continue
        if char.isspace():
            kept.append(" ")
            continue
        if unicodedata.category(char).startswith("P"):
            continue
        kept.append(char)
    collapsed = _WHITESPACE.sub(" ", "".join(kept)).strip()
    collapsed = _INFIX_APOSTROPHE.sub("", collapsed)
    return _WHITESPACE.sub(" ", collapsed).strip()


def _is_section_marker(stripped_line: str) -> bool:
    return _SECTION_MARKER.fullmatch(stripped_line) is not None


def parse_lyric_lines(lyrics: str) -> tuple[LyricLine, ...]:
    lines: list[LyricLine] = []
    for original_index, raw in enumerate(lyrics.splitlines()):
        stripped = raw.strip()
        if not stripped or _is_section_marker(stripped):
            continue
        normalized = normalize_text(stripped)
        if not normalized:
            continue
        lines.append(LyricLine(original_index=original_index, normalized=normalized))
    return tuple(lines)


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _prepare_cues(cues: Sequence[WhisperCue]) -> tuple[_PreparedCue, ...]:
    ordered = sorted(
        enumerate(cues),
        key=lambda item: (item[1].start, item[1].end, item[0]),
    )
    prepared: list[_PreparedCue] = []
    for _, cue in ordered:
        normalized = normalize_text(cue.text)
        if not normalized:
            continue
        prepared.append(
            _PreparedCue(start=cue.start, end=cue.end, normalized=normalized),
        )
    return tuple(prepared)


def _choose_unique_line(
    scores: dict[int, float],
    lines: tuple[LyricLine, ...],
    min_ratio: float,
    ambiguity_margin: float,
) -> int | None:
    if not scores:
        return None
    best_line = min(scores, key=lambda index: (-scores[index], index))
    best = scores[best_line]
    if best < min_ratio:
        return None
    best_text = lines[best_line].normalized
    competitors = [
        score
        for index, score in scores.items()
        if lines[index].normalized != best_text
    ]
    if not competitors:
        return best_line
    second_best = max(competitors)
    if best - second_best < ambiguity_margin:
        return None
    return best_line


def _map_greedy_monotone(
    lines: tuple[LyricLine, ...],
    cues: tuple[_PreparedCue, ...],
    min_ratio: float,
    ambiguity_margin: float,
) -> list[int | None]:
    mapping: list[int | None] = [None] * len(cues)
    next_unused = 0
    line_count = len(lines)
    for cue_index, cue in enumerate(cues):
        if next_unused >= line_count:
            break
        scores = {
            index: _ratio(cue.normalized, lines[index].normalized)
            for index in range(next_unused, line_count)
        }
        chosen = _choose_unique_line(scores, lines, min_ratio, ambiguity_margin)
        if chosen is None:
            continue
        mapping[cue_index] = chosen
        next_unused = chosen + 1
    return mapping


def _map_cover_then_split(
    lines: tuple[LyricLine, ...],
    cues: tuple[_PreparedCue, ...],
    min_ratio: float,
    ambiguity_margin: float,
) -> list[int | None]:
    mapping: list[int | None] = [None] * len(cues)
    if not lines:
        return mapping
    next_unused = 0
    last_assigned: int | None = None
    covered: dict[int, list[str]] = {}
    line_count = len(lines)

    for cue_index, cue in enumerate(cues):
        can_merge = False
        merge_score = 0.0
        if last_assigned is not None:
            previous = _ratio(
                " ".join(covered[last_assigned]),
                lines[last_assigned].normalized,
            )
            merge_score = _ratio(
                " ".join([*covered[last_assigned], cue.normalized]),
                lines[last_assigned].normalized,
            )
            can_merge = merge_score >= min_ratio and merge_score >= previous

        unused_scores = {
            index: _ratio(cue.normalized, lines[index].normalized)
            for index in range(next_unused, line_count)
        }
        unused_choice = _choose_unique_line(
            unused_scores, lines, min_ratio, ambiguity_margin,
        )

        if can_merge:
            unused_score = (
                unused_scores[unused_choice] if unused_choice is not None else 0.0
            )
            if unused_choice is not None and unused_score > merge_score:
                mapping[cue_index] = unused_choice
                last_assigned = unused_choice
                next_unused = unused_choice + 1
                covered[unused_choice] = [cue.normalized]
            else:
                mapping[cue_index] = last_assigned
                covered[last_assigned].append(cue.normalized)
            continue

        if unused_choice is None:
            continue
        mapping[cue_index] = unused_choice
        last_assigned = unused_choice
        next_unused = unused_choice + 1
        covered[unused_choice] = [cue.normalized]
    return mapping


def _intervals_from_mapping(
    lines: tuple[LyricLine, ...],
    cues: tuple[_PreparedCue, ...],
    mapping: Sequence[int | None],
) -> tuple[LineInterval, ...]:
    grouped: dict[int, list[tuple[float, float]]] = {}
    for cue, alignable_index in zip(cues, mapping, strict=True):
        if alignable_index is None:
            continue
        original_index = lines[alignable_index].original_index
        grouped.setdefault(original_index, []).append((cue.start, cue.end))
    return tuple(
        LineInterval(
            line_index=original_index,
            start=min(start for start, _end in spans),
            end=max(end for _start, end in spans),
        )
        for original_index, spans in sorted(grouped.items())
    )


def align(
    lyrics: str,
    cues: Sequence[WhisperCue] | None,
    candidate: str,
    min_ratio: float = MIN_RATIO,
    ambiguity_margin: float = AMBIGUITY_MARGIN,
) -> Alignment:
    if candidate not in CANDIDATES:
        raise ValueError(f"unknown candidate: {candidate}")
    lyric_lines = parse_lyric_lines(lyrics)
    prepared = () if cues is None else _prepare_cues(cues)
    if (
        candidate == CANDIDATE_NO_HIGHLIGHT
        or not lyric_lines
        or not prepared
    ):
        return Alignment(candidate=candidate, intervals=())
    if candidate == CANDIDATE_GREEDY_MONOTONE:
        mapping = _map_greedy_monotone(
            lyric_lines, prepared, min_ratio, ambiguity_margin,
        )
    elif candidate == CANDIDATE_COVER_THEN_SPLIT:
        mapping = _map_cover_then_split(
            lyric_lines, prepared, min_ratio, ambiguity_margin,
        )
    else:
        raise ValueError(f"unknown candidate: {candidate}")
    return Alignment(
        candidate=candidate,
        intervals=_intervals_from_mapping(lyric_lines, prepared, mapping),
    )


def mapped_original_indices(alignment: Alignment) -> frozenset[int]:
    return frozenset(interval.line_index for interval in alignment.intervals)


def score_alignment(
    case: CorpusCase,
    alignment: Alignment,
) -> CaseScore:
    predicted = mapped_original_indices(alignment)
    expected = case.expected_mapped_original_indices
    false_positives = predicted - expected
    true_positives = predicted & expected
    missed = expected - predicted
    return CaseScore(
        case_name=case.name,
        candidate=alignment.candidate,
        false_positive_count=len(false_positives),
        true_positive_count=len(true_positives),
        missed_count=len(missed),
        expected_mapped_count=len(expected),
    )


def _three_content_lines() -> str:
    return (
        "the zinc lantern leans\n"
        "marble moths in the rafters\n"
        "we count the quiet bells\n"
    )


def _marked_repeated_lyrics() -> str:
    return (
        "[verse]\n"
        "the zinc lantern leans\n"
        "marble moths in the rafters\n"
        "\n"
        "[chorus]\n"
        "hold the marble line\n"
        "we don't fold tonight\n"
        "\n"
        "[verse]\n"
        "another mile of yellow signs\n"
        "\n"
        "[chorus]\n"
        "hold the marble line\n"
        "we don't fold tonight\n"
    )


def synthetic_corpus() -> tuple[CorpusCase, ...]:
    three = _three_content_lines()
    exact_cues = (
        WhisperCue(0.0, 1.2, "the zinc lantern leans"),
        WhisperCue(1.2, 2.5, "marble moths in the rafters"),
        WhisperCue(2.5, 3.8, "we count the quiet bells"),
    )
    marked = _marked_repeated_lyrics()
    return (
        CorpusCase(
            name="exact_line_matches",
            lyrics=three,
            cues=exact_cues,
            expected_mapped_original_indices=frozenset({0, 1, 2}),
        ),
        CorpusCase(
            name="repeated_chorus",
            lyrics=marked,
            cues=(
                WhisperCue(0.0, 1.0, "the zinc lantern leans"),
                WhisperCue(1.0, 2.0, "marble moths in the rafters"),
                WhisperCue(2.0, 3.0, "hold the marble line"),
                WhisperCue(3.0, 4.0, "we don't fold tonight"),
                WhisperCue(4.0, 5.0, "another mile of yellow signs"),
                WhisperCue(5.0, 6.0, "hold the marble line"),
                WhisperCue(6.0, 7.0, "we don't fold tonight"),
            ),
            expected_mapped_original_indices=frozenset({1, 2, 5, 6, 9, 12, 13}),
        ),
        CorpusCase(
            name="omitted_lyric_line",
            lyrics=three,
            cues=(
                WhisperCue(0.0, 1.0, "the zinc lantern leans"),
                WhisperCue(1.0, 2.0, "we count the quiet bells"),
            ),
            expected_mapped_original_indices=frozenset({0, 2}),
        ),
        CorpusCase(
            name="extra_adlib_cue",
            lyrics=three,
            cues=(
                WhisperCue(0.0, 1.0, "the zinc lantern leans"),
                WhisperCue(1.0, 1.4, "ooh yeah come on"),
                WhisperCue(1.4, 2.6, "marble moths in the rafters"),
                WhisperCue(2.6, 3.8, "we count the quiet bells"),
            ),
            expected_mapped_original_indices=frozenset({0, 1, 2}),
        ),
        CorpusCase(
            name="section_markers_ignored",
            lyrics="[verse]\nthe zinc lantern leans\n[chorus]\nhold the marble line\n",
            cues=(
                WhisperCue(0.0, 1.0, "the zinc lantern leans"),
                WhisperCue(1.0, 2.0, "hold the marble line"),
            ),
            expected_mapped_original_indices=frozenset({1, 3}),
        ),
        CorpusCase(
            name="divergent_whisper_text",
            lyrics=three,
            cues=(
                WhisperCue(0.0, 1.0, "the zinc lantern leans"),
                WhisperCue(1.0, 2.0, "totally unrelated kitchen inventory"),
                WhisperCue(2.0, 3.0, "we count the quiet bells"),
            ),
            expected_mapped_original_indices=frozenset({0, 2}),
        ),
        CorpusCase(
            name="missing_cues",
            lyrics=three,
            cues=None,
            expected_mapped_original_indices=frozenset(),
        ),
        CorpusCase(
            name="empty_lyrics",
            lyrics="",
            cues=exact_cues,
            expected_mapped_original_indices=frozenset(),
        ),
        CorpusCase(
            name="empty_normalized_cues",
            lyrics=three,
            cues=(
                WhisperCue(0.0, 1.0, "..."),
                WhisperCue(1.0, 2.0, "!!!"),
                WhisperCue(2.0, 3.0, "???"),
            ),
            expected_mapped_original_indices=frozenset(),
        ),
        CorpusCase(
            name="ambiguous_pair",
            lyrics="alpha bravo charlie stone\nalpha bravo charlie stones\n",
            cues=(WhisperCue(0.0, 1.0, "alpha bravo charlie stone"),),
            expected_mapped_original_indices=frozenset(),
        ),
        CorpusCase(
            name="split_cues_same_line",
            lyrics="the zinc lantern leans over marble moths\n",
            cues=(
                WhisperCue(0.0, 1.1, "the zinc lantern leans over"),
                WhisperCue(1.1, 2.4, "marble moths"),
            ),
            expected_mapped_original_indices=frozenset({0}),
        ),
        CorpusCase(
            name="all_ratios_below_min",
            lyrics=three,
            cues=(
                WhisperCue(0.0, 1.0, "zzzz qqqq xxxx"),
                WhisperCue(1.0, 2.0, "jjjj kkkk mmmm"),
            ),
            expected_mapped_original_indices=frozenset(),
        ),
    )


def evaluate_synthetic_corpus(
    min_ratio: float = MIN_RATIO,
    ambiguity_margin: float = AMBIGUITY_MARGIN,
) -> EvalSummary:
    scores: list[CaseScore] = []
    for case in synthetic_corpus():
        for candidate in CANDIDATES:
            alignment = align(
                case.lyrics,
                case.cues,
                candidate,
                min_ratio=min_ratio,
                ambiguity_margin=ambiguity_margin,
            )
            scores.append(score_alignment(case, alignment))
    return EvalSummary(
        min_ratio=min_ratio,
        ambiguity_margin=ambiguity_margin,
        scores=tuple(scores),
    )


def format_eval_summary(summary: EvalSummary) -> str:
    case_names = {score.case_name for score in summary.scores}
    lines = [
        (
            f"synthetic-eval min_ratio={summary.min_ratio} "
            f"ambiguity_margin={summary.ambiguity_margin} "
            f"cases={len(case_names)}"
        ),
    ]
    for candidate in CANDIDATES:
        false_positives, true_positives, missed = summary.totals(candidate)
        lines.append(
            f"{candidate} false_positives={false_positives} "
            f"true_positives={true_positives} missed={missed}"
        )
    lines.append("per-case")
    for score in summary.scores:
        lines.append(
            f"{score.case_name} {score.candidate} "
            f"false_positives={score.false_positive_count} "
            f"true_positives={score.true_positive_count} "
            f"missed={score.missed_count}"
        )
    return "\n".join(lines) + "\n"


def corpus_hash(whisper_text: str, lyrics: str) -> str:
    payload = f"{whisper_text}{lyrics}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def is_postgres_url(url: str) -> bool:
    lowered = url.strip().lower()
    return lowered.startswith(_POSTGRES_PREFIXES)


def postgres_dsn(url: str) -> str:
    stripped = url.strip()
    if stripped.startswith(_SQLALCHEMY_PSYCOPG2_PREFIX):
        return _PSYCOPG2_PREFIX + stripped[len(_SQLALCHEMY_PSYCOPG2_PREFIX):]
    return stripped


def _connect_postgres(dsn: str) -> object:
    import psycopg2

    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def run_live_probe(database_url: str | None) -> int:
    if database_url is None or not database_url.strip():
        print(DATABASE_URL_MISSING, file=sys.stderr)
        return 2
    if not is_postgres_url(database_url):
        print(DATABASE_URL_NOT_POSTGRES, file=sys.stderr)
        return 2

    try:
        conn = _connect_postgres(postgres_dsn(database_url))
    except Exception as exc:
        print(f"live-probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    try:
        cursor = conn.cursor()
        try:
            cursor.execute(_LIVE_COUNTS_SQL)
            generations, whisper_text, cues_nonnull, cues_nonempty = cursor.fetchone()
            cursor.execute(_LIVE_HASH_SQL)
            hash_rows = cursor.fetchall()
        finally:
            cursor.close()
    except Exception as exc:
        print(f"live-probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print("live-probe")
    print(f"generations={generations}")
    print(f"whisper_text={whisper_text}")
    print(f"whisper_cues_nonnull={cues_nonnull}")
    print(f"whisper_cues_nonempty={cues_nonempty}")
    if cues_nonempty == 0:
        print(NO_WHISPER_CUES)
    else:
        print(CUES_PRESENT_NO_ALIGN)
    print("generation_id sha256")
    for generation_id, whisper_text_value, lyrics_value in hash_rows:
        digest = corpus_hash(whisper_text_value or "", lyrics_value or "")
        print(f"{generation_id} {digest}")
    return 0


def run_synthetic(
    min_ratio: float = MIN_RATIO,
    ambiguity_margin: float = AMBIGUITY_MARGIN,
) -> int:
    summary = evaluate_synthetic_corpus(
        min_ratio=min_ratio,
        ambiguity_margin=ambiguity_margin,
    )
    sys.stdout.write(format_eval_summary(summary))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("synthetic", help="evaluate the invented offline corpus")
    subparsers.add_parser(
        "live-probe",
        help="count live whisper fields and print generation hashes only",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "synthetic":
        return run_synthetic()
    if args.command == "live-probe":
        return run_live_probe(os.environ.get("DATABASE_URL"))
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
