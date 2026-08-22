"""Print the two numbers `WS-H5` is about: coverage, and requests per turn.

`WS-H5` criteria 3 and 4 ask the acceptance gate to PRINT a fan-out's coverage
and the per-turn upstream request count, not merely to assert them somewhere.
`apps/api/tests/unit/test_interpreter_middleware.py::
test_one_eval_covers_every_item_at_a_fixed_request_count` already asserts both
and stays the pin; this is the instrument that says them out loud, because the
argument the workstream exists to settle is a comparison of two numbers and a
green tick is not a number.

**What is being compared.** A model-driven fan-out over a list samples it — it
inspects three items and answers as though it inspected twenty — and issues an
unpredictable number of upstream chat completions doing it. The interpreter
loop iterates the list in QuickJS, calling the bridged tool per item with no
model in the loop, so the gateway sees exactly two completions: the turn that
emitted the `eval` call, and the turn that read its result and answered. That
number does not move when 20 becomes 200.

**How it is measured.** The real `build_interpreter_middleware`, the real
`guarded_ptc_tools`, a real compiled deepagents graph, against
`aleph_models.testing.FakeGateway` with the model's two replies scripted. No
socket is opened, no gateway is needed and no tokens are spent, which is why
this can run on every acceptance pass rather than behind an opt-in like `H2`.
Both numbers are read from the transport — the tool calls from the stub the
REPL actually invoked, the completions from
`FakeGateway.count("/v1/chat/completions")` — never from anything the agent
reports about itself.

**Which half of the pair carries the weight, stated plainly.** With the model's
two replies scripted, the completion count is 2 unless something outside the
model loop talks to the gateway; no small regression in `interpreter.py` moves
it, and mutation runs confirm that (a PTC budget of 5, and the interpreter
middleware removed entirely, both leave it at 2). So the completion count on its
own is a weak assertion, and calling it the finding would be this repository's
favourite mistake.

The number that discriminates is the **ratio**. Twenty tool calls arrived at the
gateway's expense of two completions. A model-driven fan-out cannot do that: a
tool call the model issues costs a completion, so twenty of them cost at least
twenty. `tool calls per completion` is therefore the quantity that separates the
two designs, it is bounded above by 1 for the alternative, and it is what this
probe prints and gates on. The completion count is the denominator, reported
because a ratio with an unstated denominator is not evidence.

Exit status is 0 only when the fan-out covered every item, touched each exactly
once, the per-turn completion count was identical across all three runs, and the
tool-calls-per-completion ratio was above what a model-driven loop can reach.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

#: Items in the scripted fan-out. `WS-H5` c3 names 20; the point of the
#: workstream is that the request count below is independent of this.
ITEM_COUNT = 20

#: Turns of the fan-out, so "identical across runs" is a claim about a sample
#: rather than about one observation.
RUNS = 3

#: What the gateway should see per turn: the completion that emitted the `eval`
#: call, and the completion that read its result and answered. Written as a
#: number rather than derived from the scripted-reply list, because deriving it
#: from the fixture would make this agree with itself: a loop that issued one
#: completion per item would exhaust the script, and "it used everything I gave
#: it" is not the claim.
EXPECTED_REQUESTS_PER_TURN = 2

#: The most tool calls a model-driven fan-out can get per upstream chat
#: completion. Exactly one: a tool call the model issues IS a completion, so N
#: tool calls cost at least N. Anything above this ceiling could only have come
#: from a loop running below the model, which is what `WS-H5` is about. This is
#: the discriminating measurement; the raw completion count is its denominator.
MODEL_DRIVEN_CEILING = 1.0

#: The JS the model is scripted to emit. It iterates the list itself and calls
#: the bridged `search_wiki` once per item, which is the behaviour under test.
FANOUT_SOURCE = """
const items = Array.from({length: %(n)d}, (_, i) => `item-${i}`);
const out = [];
for (const it of items) { out.push(await tools.searchWiki({query: it})); }
`covered ${out.length}/${items.length}`;
"""

_THREAD_ID = "proj:11111111-1111-1111-1111-111111111111:probe"

#: Stand-in for the eval tool's text when the scripted turn never reached the
#: interpreter at all.
NO_INTERPRETER = "<the scripted turn never reached the interpreter>"

_MISSING_QUICKJS = (
    "langchain-quickjs is not installed. It is a pinned runtime dependency of "
    "aleph-api (`langchain-quickjs~=0.2.0`); without it the API cannot build "
    "its assistant agent at all. Run `uv sync --all-packages --all-extras`."
)


def _eval_call(code: str) -> dict[str, Any]:
    """A chat completion whose only content is one call to the interpreter."""
    import json

    from aleph_api.interpreter import INTERPRETER_TOOL_NAME

    return {
        "id": "chatcmpl-eval",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-chat",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-eval",
                            "type": "function",
                            "function": {
                                "name": INTERPRETER_TOOL_NAME,
                                "arguments": json.dumps({"code": code}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _plain_answer(text: str) -> dict[str, Any]:
    """A chat completion that answers and calls nothing."""
    return {
        "id": "chatcmpl-answer",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-chat",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _toolset(touched: list[str]) -> list[Any]:
    """One tool per allowlisted name, with `search_wiki` recording what it saw.

    `guarded_ptc_tools` refuses a name in `PTC_ALLOWLIST` it cannot resolve, so
    the probe has to carry the whole allowlist rather than invent one name —
    which also means it runs against the real set of tools a loop may call.
    """
    from langchain_core.tools import tool

    from aleph_api.interpreter import PTC_ALLOWLIST

    async def _stub(arg: str = "") -> str:
        """Stand-in for an allowlisted read-only orchestrator tool."""
        return "ok"

    async def look_up(query: str, top_k: int = 6) -> str:
        """Scan the wiki for one query."""
        touched.append(query)
        return f"{query}: ok"

    built: list[Any] = []
    for name in PTC_ALLOWLIST:
        made = tool(look_up if name == "search_wiki" else _stub)
        made.name = name
        built.append(made)
    return built


async def _one_turn(code: str) -> tuple[str, int, list[str]]:
    """Drive one turn that calls the interpreter once, and count the traffic."""
    from deepagents import create_deep_agent
    from langchain_openai import ChatOpenAI

    from aleph_api.interpreter import INTERPRETER_TOOL_NAME, build_interpreter_middleware
    from aleph_models.testing import FakeGateway, GatewayConfig, ScriptedResponse

    touched: list[str] = []
    tools = _toolset(touched)

    fake = FakeGateway(
        GatewayConfig.well_behaved(
            invoke_script=(
                ScriptedResponse(status=200, body=_eval_call(code)),
                ScriptedResponse(status=200, body=_plain_answer("done")),
            )
        )
    )
    http = fake.client()
    try:
        model = ChatOpenAI(
            model="fake-chat",
            api_key="sk-fake-virtual-key",  # ty: ignore[invalid-argument-type]
            base_url=fake.base_url + "/v1",
            http_async_client=http,
            max_retries=0,
        )
        agent = create_deep_agent(
            model=model,
            tools=tools,
            middleware=build_interpreter_middleware(tools=tools),
        )
        out = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "cover the list"}]},
            config={"configurable": {"thread_id": _THREAD_ID}},
        )
    finally:
        await http.aclose()

    evals = [m for m in out["messages"] if getattr(m, "name", None) == INTERPRETER_TOOL_NAME]
    # Not an exception. A turn that never reached the interpreter is a result
    # this probe exists to report, and the request count it measured is the most
    # interesting half of it — raising here would throw that number away and
    # print a traceback in the acceptance row instead of a measurement.
    text = str(evals[0].content) if evals else NO_INTERPRETER
    return text, fake.count("/v1/chat/completions"), list(touched)


async def _probe(items: int, runs: int) -> int:
    import importlib.util

    if importlib.util.find_spec("langchain_quickjs") is None:
        print(f"FAIL: {_MISSING_QUICKJS}")
        return 1

    code = FANOUT_SOURCE % {"n": items}
    expected_cover = f"covered {items}/{items}"
    counts: list[int] = []
    covered: list[int] = []
    failures: list[str] = []

    for run in range(1, runs + 1):
        text, requests, touched = await _one_turn(code)
        counts.append(requests)
        distinct = len(set(touched))
        covered.append(distinct)
        ratio = distinct / requests if requests else 0.0
        print(
            f"  run {run}: {text.strip()!r} — {len(touched)} tool calls "
            f"({distinct} distinct), {requests} upstream chat completion(s), "
            f"{ratio:.1f} tool calls per completion"
        )
        if expected_cover not in text:
            failures.append(f"run {run} returned {text.strip()!r}, not {expected_cover!r}")
        if len(touched) != items or distinct != items:
            failures.append(
                f"run {run} touched {len(touched)} item(s), {distinct} distinct, of {items}"
            )
        if ratio <= MODEL_DRIVEN_CEILING:
            failures.append(
                f"run {run} made {distinct} tool call(s) for {requests} upstream "
                f"completion(s) — a ratio of {ratio:.1f}, which a model-driven fan-out "
                f"reaches (its ceiling is {MODEL_DRIVEN_CEILING:g}); the loop is not "
                "carrying the fan-out"
            )

    if len(set(counts)) != 1:
        failures.append(
            f"the per-turn upstream completion count moved between runs ({counts}); "
            "a fixed number across runs is the criterion, and model-driven fan-out is not"
        )
    elif counts[0] != EXPECTED_REQUESTS_PER_TURN:
        failures.append(
            f"each turn issued {counts[0]} upstream chat completion(s), expected "
            f"{EXPECTED_REQUESTS_PER_TURN} (the eval turn and the answer turn); a count "
            "that scales with the item count means the model is driving the fan-out"
        )

    # The summary goes LAST and carries every number on one line, because
    # `run_shell` in `scripts/acceptance.sh` records only the last non-empty
    # line of a row's output. A verdict word there ("OK") would satisfy the
    # criterion's letter and print no number at all, which is the whole thing
    # `WS-H5` c3/c4 asked for. It reports what was MEASURED, not what was
    # wanted: a summary that says "covered 20/20" on a run that covered five is
    # the failure mode this gate exists to stop.
    spread = ", ".join(str(c) for c in counts)
    best = max(covered) if covered else 0
    worst = min(covered) if covered else 0
    reach = f"{worst}/{items}" if worst == best else f"{worst}-{best}/{items}"
    summary = (
        f"fan-out covered {reach} items per turn on {spread} upstream chat "
        f"completion(s) over {runs} runs"
    )
    if failures:
        for line in failures:
            print(f"FAIL: {line}")
        # The summary goes LAST on BOTH paths. `run_shell` records the last
        # non-empty line as the row's detail, so printing it first on failure
        # meant the gate showed `D9 FAIL  FAIL: run 3 touched 5 item(s)…` and
        # the completion count — the number the criterion is ABOUT — never
        # reached the row. A failing row that omits the measurement is a row
        # that tells you something broke and not what.
        print(f"{summary} — NOT the criterion")
        return 1
    print(f"{summary} — identical, {best / counts[0]:.0f} tool calls per completion")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=ITEM_COUNT)
    parser.add_argument("--runs", type=int, default=RUNS)
    args = parser.parse_args()

    import asyncio

    return asyncio.run(_probe(args.items, args.runs))


if __name__ == "__main__":
    sys.exit(main())
