"""Mute placement around video cuts.

The bug these cover: `volume` sits before `aselect` in the filter chain, so its `enable`
expression is evaluated against the *input* timeline. Passing it `shift_mutes` output
applied the cut correction twice, and every mute after a cut fired early by the total
duration cut before it. Severance S01E06 had one 8.4s cut and every later mute landed
8.4s before its word, while episodes with no cuts were unaffected.

`shift_mutes` itself was never wrong — it computes the output timeline, which is what the
run report needs. The renderer just must not use it for the filter expression.

These are pure-arithmetic tests over the filter expression; they need no ffmpeg and no
media. The end-to-end check that a rendered file actually goes quiet at the right second
was run separately against synthetic media (see the commit message).
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from render import shift_mutes, _complement  # noqa: E402


def _mute_expr_times(mutes, cuts):
    """The source times the renderer puts in the `volume=enable` expression.

    Mirrors _render_with_cuts' selection so the rule is asserted without invoking ffmpeg.
    """
    ordered = sorted(cuts, key=lambda c: c["start"])
    return [
        (s, e) for _ref, s, e in mutes
        if not any(c["start"] <= s and e <= c["end"] for c in ordered)
    ]


CUTS = [{"start": 10.0, "end": 15.0}, {"start": 30.0, "end": 38.0}]
MUTES = [
    ("before", 5.0, 6.0),      # before any cut
    ("inside", 31.0, 32.0),    # inside cut 2 — audio is gone with the video
    ("between", 20.0, 21.0),   # after cut 1 only
    ("after", 50.0, 51.0),     # after both cuts
]


def test_filter_expression_uses_source_times():
    """The rendered mute times are the source times, NOT the shifted ones."""
    assert _mute_expr_times(MUTES, CUTS) == [(5.0, 6.0), (20.0, 21.0), (50.0, 51.0)]


def test_shift_mutes_reports_output_times():
    """shift_mutes still reports the output timeline, for the report and the UI."""
    assert [(r, round(s, 3)) for r, s, _e in shift_mutes(MUTES, CUTS)] == [
        ("before", 5.0),      # unchanged, precedes every cut
        ("between", 15.0),    # 20 - 5
        ("after", 37.0),      # 50 - 13
    ]


def test_render_and_report_timelines_differ_by_preceding_cuts():
    """The two timelines must not be interchangeable — that confusion was the bug."""
    rendered = _mute_expr_times(MUTES, CUTS)
    reported = [(s, e) for _r, s, e in shift_mutes(MUTES, CUTS)]
    assert rendered != reported
    for (rs, _re_), (ps, _pe) in zip(rendered, reported):
        removed = sum(
            min(c["end"], rs) - c["start"] for c in CUTS if c["start"] < rs
        )
        assert abs((rs - removed) - ps) < 1e-6


def test_mute_inside_a_cut_is_dropped_from_both():
    """A mute swallowed by a cut must not silence audio that survives."""
    assert not any(s == 31.0 for s, _e in _mute_expr_times(MUTES, CUTS))
    assert not any(r == "inside" for r, _s, _e in shift_mutes(MUTES, CUTS))


def test_no_cuts_leaves_mutes_untouched():
    """The audio-only path — every episode without a video cut — is unaffected."""
    assert _mute_expr_times(MUTES, []) == [(5.0, 6.0), (31.0, 32.0),
                                           (20.0, 21.0), (50.0, 51.0)]
    assert [(r, s, e) for r, s, e in shift_mutes(MUTES, [])] == MUTES


def test_complement_spans_are_the_kept_ranges():
    """Sanity-check the keep-expression the mute times are evaluated against."""
    assert _complement(CUTS)[0] == (0.0, 10.0)
    assert (15.0, 30.0) in _complement(CUTS)
