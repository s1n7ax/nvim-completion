from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from difflib import SequenceMatcher
from math import inf
from typing import Iterable, Iterator, MutableSequence, Sequence, Tuple

from std2.concurrent.futures import gather

from ...agnostic.datatypes import Completion, Context
from ...agnostic.parse import is_word
from ...agnostic.settings.types import Options, Weights
from ..model.database import Database, SqlMetrics


class _ToleranceExceeded(Exception):
    pass


@dataclass(frozen=True)
class _MatchMetrics:
    prefix_matches: int
    consecutive_matches: int
    num_matches: int


def _isjunk(s: str) -> bool:
    return s.isspace()


def _secondary(n_cword: str, n_match: str) -> _MatchMetrics:
    m = SequenceMatcher(a=n_cword, b=n_match, autojunk=True, isjunk=_isjunk)
    matches: MutableSequence[int] = []
    prefix_matches = 0
    num_matches = 0
    consecutive_matches = 0

    for ai, bi, size in m.get_matching_blocks():
        num_matches += size
        if ai == bi == 0:
            prefix_matches = size
        for i in range(bi, bi + size):
            matches.append(i)

    pm_idx = inf
    for i in matches:
        if pm_idx == i - 1:
            consecutive_matches += 1
        pm_idx = i

    metric = _MatchMetrics(
        prefix_matches=prefix_matches,
        consecutive_matches=consecutive_matches,
        num_matches=num_matches,
    )
    return metric


def _primary(
    transpose_band: int,
    cword: str,
    match: str,
    n_match: str,
) -> _MatchMetrics:

    idx = 0
    prefix_broken = False
    pm_idx = inf
    prefix_matches = 0
    consecutive_matches = 0
    num_matches = 0

    for i, char in enumerate(cword):
        if i > transpose_band and not num_matches:
            raise _ToleranceExceeded()
        else:
            target = match if char.isupper() else n_match
            m_idx = target.find(char, idx, idx + transpose_band)

            if m_idx != -1:
                if pm_idx == m_idx - 1:
                    consecutive_matches += 1
                num_matches += 1
                pm_idx = m_idx
                idx = m_idx + 1
            if m_idx != i:
                prefix_broken = True
            if not prefix_broken:
                prefix_matches += 1

    metric = _MatchMetrics(
        prefix_matches=prefix_matches,
        consecutive_matches=consecutive_matches,
        num_matches=num_matches,
    )
    return metric


def _metrics(
    options: Options, context: Context, completions: Iterable[Completion]
) -> Iterator[_MatchMetrics]:
    w_before = context.words_before, context.words_before.casefold()
    sw_before = context.syms_before, context.syms_before.casefold()

    for completion in completions:
        edit = completion.primary_edit
        match, n_match = edit.new_text, edit.new_text.casefold()
        word_start = is_word(match[:1], unifying_chars=options.unifying_chars)
        cword, n_cword = w_before if word_start else sw_before

        try:
            yield _primary(
                options.transpose_band, cword=cword, match=match, n_match=n_match
            )
        except _ToleranceExceeded:
            yield _secondary(n_cword, n_match=n_match)


def _talley(
    weights: Weights, metrics: Sequence[Tuple[Completion, SqlMetrics, _MatchMetrics]]
) -> Iterator[Completion]:
    insertion_order = 0
    ft_count = 0
    line_diff = 0

    prefix_matches = 0
    consecutive_matches = 0
    num_matches = 0

    for _, sql, match in metrics:
        insertion_order += sql["insertion_order"]
        ft_count += sql["ft_count"]
        line_diff += sql["line_diff"]

        prefix_matches += match.prefix_matches
        consecutive_matches += match.consecutive_matches
        num_matches += match.num_matches

    def key_by(metric: Tuple[Completion, SqlMetrics, _MatchMetrics]) -> float:
        _, sql, match = metric
        return (
            (weights.insertion_order * sql["insertion_order"] / insertion_order)
            + (weights.count_by_filetype * sql["ft_count"] / ft_count)
            + (weights.nearest_neighbour * sql["line_diff"] / line_diff)
            + (weights.prefix_matches * match.prefix_matches / match.prefix_matches)
            + (
                weights.consecutive_matches
                * match.consecutive_matches
                / consecutive_matches
            )
            + (weights.num_matches * match.num_matches / num_matches)
        )

    completions = (comp for comp, _, _ in sorted(metrics, key=key_by))
    return completions


def rank(
    pool: ThreadPoolExecutor,
    options: Options,
    weights: Weights,
    db: Database,
    context: Context,
    completions: Sequence[Completion],
) -> Iterator[Completion]:
    def c1() -> Sequence[SqlMetrics]:
        words = (comp.primary_edit.new_text for comp in completions)
        return db.metric(
            words,
            filetype=context.filetype,
            filename=context.filename,
            line_num=context.line_num,
        )

    def c2() -> Sequence[_MatchMetrics]:
        return tuple(_metrics(options, context=context, completions=completions))

    f1, f2 = pool.submit(c1), pool.submit(c2)
    gather(f1, f2)
    metrics = tuple(zip(completions, f1.result(), f2.result()))
    return _talley(weights, metrics=metrics)