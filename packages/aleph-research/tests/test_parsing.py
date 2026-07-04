"""Plan / reflect / triage LLM-response parsing, incl. malformed-JSON tolerance."""

from __future__ import annotations

from aleph_research.research_workflow import parse_plan, parse_reflect, parse_triage


class TestParsePlan:
    def test_well_formed(self) -> None:
        text = (
            '{"subqueries": [{"query": "quantum radar basics", "tools": ["tavily"]}, '
            '{"query": "quantum illumination", "tools": []}]}'
        )
        subs = parse_plan(text, topic="Quantum radar", bound_kinds=["tavily", "openalex"])
        assert [s.query for s in subs] == ["quantum radar basics", "quantum illumination"]
        assert subs[0].tools == ("tavily",)
        assert subs[1].tools == ()

    def test_fenced_json_recovered(self) -> None:
        text = 'Sure!\n```json\n{"subqueries": [{"query": "q1"}]}\n```'
        subs = parse_plan(text, topic="T", bound_kinds=[])
        assert [s.query for s in subs] == ["q1"]

    def test_unknown_tools_filtered(self) -> None:
        text = '{"subqueries": [{"query": "q", "tools": ["tavily", "nonsense"]}]}'
        subs = parse_plan(text, topic="T", bound_kinds=["tavily"])
        assert subs[0].tools == ("tavily",)

    def test_string_items_accepted(self) -> None:
        subs = parse_plan('{"subqueries": ["plain query"]}', topic="T", bound_kinds=[])
        assert subs[0].query == "plain query"
        assert subs[0].tools == ()

    def test_malformed_falls_back_to_topic(self) -> None:
        for text in ("", "not json at all", '{"subqueries": "oops"}', "[1, 2, 3]"):
            subs = parse_plan(text, topic="The Topic", bound_kinds=["tavily"])
            assert len(subs) == 1
            assert subs[0].query == "The Topic"

    def test_caps_at_six(self) -> None:
        items = ", ".join(f'{{"query": "q{i}"}}' for i in range(10))
        subs = parse_plan(f'{{"subqueries": [{items}]}}', topic="T", bound_kinds=[])
        assert len(subs) == 6


class TestParseReflect:
    def test_done(self) -> None:
        assert parse_reflect('{"done": true, "gaps": []}') == (True, [])

    def test_gaps(self) -> None:
        done, gaps = parse_reflect('{"done": false, "gaps": ["a", "b"]}')
        assert done is False
        assert gaps == ["a", "b"]

    def test_no_gaps_means_done(self) -> None:
        assert parse_reflect('{"done": false, "gaps": []}')[0] is True

    def test_malformed_means_done(self) -> None:
        for text in ("", "garbage", '{"done": false', "[]"):
            assert parse_reflect(text) == (True, [])


class TestParseTriage:
    def test_well_formed(self) -> None:
        assert parse_triage('{"selected": [2, 0]}', n_candidates=4, max_select=3) == [2, 0]

    def test_deliberate_empty_selection_honored(self) -> None:
        assert parse_triage('{"selected": []}', n_candidates=4, max_select=3) == []

    def test_out_of_range_and_dupes_dropped(self) -> None:
        out = parse_triage('{"selected": [9, 1, 1, -2, "3"]}', n_candidates=4, max_select=6)
        assert out == [1, 3]

    def test_malformed_falls_back_to_first_n(self) -> None:
        assert parse_triage("not json", n_candidates=5, max_select=3) == [0, 1, 2]

    def test_cap_applied(self) -> None:
        out = parse_triage('{"selected": [0, 1, 2, 3]}', n_candidates=4, max_select=2)
        assert out == [0, 1]
