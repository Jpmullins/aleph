"""Wave 4 T6 — delta SurfaceStreamer support: a pure JSON-pointer data-model diff.

The SSE endpoint (`apps/api/.../routes/surfaces.py`) emits the full v0_9 surface
on connect, then on each recompute computes the minimal set of changes between
the previously-sent data model and the freshly-built one and emits one
`updateDataModel` message per change. This module owns the *pure* diff so it can
be unit-tested without any I/O.

`diff_data_model(prev, nxt)` returns RFC-6902-flavoured patch ops::

    [{"op": "replace", "path": "/items/0/confidence", "value": "high"},
     {"op": "add",     "path": "/items/2",            "value": {...}},
     {"op": "remove",  "path": "/items/1"}]

Design choices (kept correct over clever):

* **Dicts** diff key-by-key: keys only in `nxt` → ``add``; keys only in `prev`
  → ``remove``; keys in both recurse (or ``replace`` if the value is a scalar /
  type changed).
* **Lists** diff index-by-index up to the shorter length (recurse per index),
  then ``add`` each surplus tail element of `nxt`, or ``remove`` each surplus
  tail element of `prev` (emitted high-index-first so applying them left to
  right stays valid).
* A **type change** (e.g. object → list, or scalar → object) at a path is a
  single ``replace`` of that whole subtree — never a partial descent.

### Mapping ops onto the v0_9 wire (remove handling)

The v0_9 server-to-client union (`@a2ui/web_core/.../server-to-client.d.ts`) has
exactly four messages: `createSurface`, `updateComponents`, `updateDataModel`,
`deleteSurface`. There is **no per-leaf remove primitive**. The upstream
`DataModel.set(path, value)` does treat ``value === undefined`` as a delete
(removes an object key; sets an array slot to a sparse ``undefined``), so:

* ``replace`` / ``add`` → ``update_data_model(path=op.path, value=op.value)``.
* ``remove`` of an **object key** → ``update_data_model(path, value=None)``
  (``None`` serialises to JSON ``null``; the binder reads it as "gone"). This is
  faithful enough for the bound props Aleph uses today.
* ``remove`` of an **array element** can NOT be expressed leaf-wise without
  leaving a sparse hole and shifting every later index's binding. So when a diff
  contains any array ``remove``, `data_model_patches_to_messages` falls back to
  re-setting the **whole changed array** with one `update_data_model` (the wire
  explicitly supports a non-root path + full array value). That is simpler and
  always correct; only the (rare) delete path pays the cost of re-sending one
  array, and the component tree is refreshed by the accompanying
  `update_components` the streamer sends alongside structural changes.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, cast

from aleph_a2ui.messages import update_data_model


def _escape(token: str) -> str:
    """Escape a single JSON-pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _is_container(v: Any) -> bool:
    return isinstance(v, (dict, list))


def diff_data_model(prev: dict[str, Any], nxt: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the minimal JSON-pointer patch list turning `prev` into `nxt`."""
    patches: list[dict[str, Any]] = []
    _diff(prev, nxt, "", patches)
    return patches


def _diff(prev: Any, nxt: Any, path: str, out: list[dict[str, Any]]) -> None:
    if prev == nxt:
        return

    # Type change (incl. container <-> scalar, dict <-> list): replace wholesale.
    if type(prev) is not type(nxt) or not _is_container(prev):
        out.append({"op": "replace", "path": path, "value": nxt})
        return

    if isinstance(prev, dict):
        # `type(prev) is type(nxt)` was checked above, so both are dicts.
        prev_d = cast("dict[str, Any]", prev)
        nxt_d = cast("dict[str, Any]", nxt)
        for key, pval in prev_d.items():
            child_path = f"{path}/{_escape(str(key))}"
            if key not in nxt_d:
                out.append({"op": "remove", "path": child_path})
            else:
                _diff(pval, nxt_d[key], child_path, out)
        for key, nval in nxt_d.items():
            if key not in prev_d:
                out.append({"op": "add", "path": f"{path}/{_escape(str(key))}", "value": nval})
        return

    # Both lists.
    prev_l = cast("list[Any]", prev)
    nxt_l = cast("list[Any]", nxt)
    common = min(len(prev_l), len(nxt_l))
    for i in range(common):
        _diff(prev_l[i], nxt_l[i], f"{path}/{i}", out)
    if len(nxt_l) > len(prev_l):
        for i in range(len(prev_l), len(nxt_l)):
            out.append({"op": "add", "path": f"{path}/{i}", "value": nxt_l[i]})
    elif len(prev_l) > len(nxt_l):
        # Remove surplus tail high-index-first so left-to-right application stays
        # valid (and so the streamer's "any array remove → re-set whole array"
        # fallback can detect array removes cleanly).
        for i in range(len(prev_l) - 1, len(nxt_l) - 1, -1):
            out.append({"op": "remove", "path": f"{path}/{i}"})


def split_surface_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Split a built v0_9 message list into (component messages, data model).

    The `_<tab>_messages` builders emit an ordered list of
    `createSurface` + `updateComponents` [+ a root `updateDataModel`]. For the
    streamer we want the *structural* messages (everything except the root
    data-model seed) and the data model itself (so we can diff it on the next
    recompute). A tab with no bound data model yields an empty dict — its stream
    just (re-)emits the structural messages and never produces a data delta.
    """
    structural: list[dict[str, Any]] = []
    data_model: dict[str, Any] = {}
    for m in messages:
        udm = m.get("updateDataModel")
        if udm is not None and udm.get("path") in ("/", "", None):
            val: Any = udm.get("value")
            data_model = cast("dict[str, Any]", val) if isinstance(val, dict) else {}
            continue
        structural.append(m)
    return structural, data_model


def _array_remove_paths(patches: list[dict[str, Any]]) -> set[str]:
    """Parent paths of any `remove` whose last token is a numeric (array index)."""
    parents: set[str] = set()
    for p in patches:
        if p["op"] != "remove":
            continue
        path = p["path"]
        head, _, last = path.rpartition("/")
        if last.isdigit():
            parents.add(head)
    return parents


def _get_at(model: dict[str, Any], pointer: str) -> Any:
    """Resolve a JSON pointer against `model` (pointer like ``/items``)."""
    cur: Any = model
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        cur = cast("Any", cur[int(token)] if isinstance(cur, list) else cur[token])
    return cur


def data_model_patches_to_messages(
    *,
    surface_id: str,
    patches: list[dict[str, Any]],
    next_model: dict[str, Any],
) -> list[dict[str, Any]]:
    """Map diff patches onto v0_9 `updateDataModel` messages.

    `next_model` is the freshly-built data model the patches produce; it is used
    for the array-remove fallback (re-set the whole changed array from the new
    model). See the module docstring for the remove-handling rationale.
    """
    if not patches:
        return []

    array_parents = _array_remove_paths(patches)
    messages: list[dict[str, Any]] = []
    reset_parents: set[str] = set()

    # For each array that had a remove, re-set the whole array once and skip any
    # other patch that targets inside it (the re-set already carries those).
    for parent in sorted(array_parents):
        messages.append(
            update_data_model(
                surface_id=surface_id,
                path=parent or "/",
                value=_get_at(next_model, parent) if parent else next_model,
            )
        )
        reset_parents.add(parent)

    def _covered(path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in reset_parents)

    for op in patches:
        if _covered(op["path"]):
            continue
        if op["op"] == "remove":
            # Object-key remove → set value to null (DataModel.set deletes the key).
            messages.append(update_data_model(surface_id=surface_id, path=op["path"], value=None))
        else:  # replace | add
            messages.append(
                update_data_model(surface_id=surface_id, path=op["path"], value=op["value"])
            )
    return messages


# ---------------------------------------------------------------------------
# Reconnect / resume / ordering — the monotonic sequence + bounded ring buffer
# (WP-4 sub-spec (a)). This is the load-bearing gap the delta substrate had:
# without a resume cursor a dropped SSE connection could silently miss deltas
# (EventSource auto-reconnect just re-opens; the server used to re-snapshot,
# tearing every card down). We stamp every SSE message with a per-connection
# `seq` (also emitted as the SSE `id:` field) and keep the last `ring_size`
# messages so a reconnect carrying `Last-Event-ID` can replay only what it
# missed, or fall back to a clean full snapshot if the gap is beyond the ring.
#
# The class is a *pure* state machine (no I/O): the route (`routes/surfaces.py`)
# owns the registry keyed by client-connection id and the SSE serialization, so
# the stamping/replay logic is unit-testable in isolation.
# ---------------------------------------------------------------------------


def stamp_seq(message: dict[str, Any], seq: int) -> dict[str, Any]:
    """Return a copy of `message` with the monotonic `seq` stamped in.

    The same integer is emitted as the SSE `id:` line so the browser
    `EventSource` echoes it back as `Last-Event-ID` on reconnect.
    """
    return {**message, "seq": seq}


class SurfaceStreamBuffer:
    """Per-connection sequence stamper + bounded replay ring.

    * `stamp(message)` assigns the next monotonic `seq`, records the stamped
      message in the ring, and returns it.
    * `can_replay(last_event_id)` is True when every message after
      `last_event_id` is still retained (no eviction gap and the id is not from
      the future) — i.e. a reconnect can be served incrementally.
    * `messages_after(last_event_id)` returns the retained tail to replay.

    `model` / `structural` hold the surface state *as last emitted* so a
    resuming generator can diff forward from exactly what the client last had
    (delivering only the deltas missed while disconnected — never a
    resnapshot). `touched` supports TTL eviction of abandoned connections in the
    route's registry.
    """

    def __init__(self, ring_size: int = 64) -> None:
        self._ring: deque[tuple[int, dict[str, Any]]] = deque(maxlen=ring_size)
        self.next_seq: int = 0
        self.model: dict[str, Any] = {}
        self.structural: list[dict[str, Any]] = []
        self.touched: float = time.monotonic()

    def stamp(self, message: dict[str, Any]) -> dict[str, Any]:
        seq = self.next_seq
        self.next_seq += 1
        stamped = stamp_seq(message, seq)
        self._ring.append((seq, stamped))
        self.touched = time.monotonic()
        return stamped

    @property
    def newest_seq(self) -> int:
        """Highest seq assigned so far (-1 before anything is stamped)."""
        return self.next_seq - 1

    @property
    def oldest_seq(self) -> int | None:
        """Lowest seq still retained in the ring (None when empty)."""
        return self._ring[0][0] if self._ring else None

    def can_replay(self, last_event_id: int) -> bool:
        """Can we serve a reconnect at `last_event_id` incrementally?"""
        if not self._ring:
            return False
        if last_event_id > self.newest_seq:
            # Client claims to have seen more than we ever sent → distrust, resync.
            return False
        oldest = self.oldest_seq
        # Every message with seq in (last_event_id, newest_seq] must be retained.
        # Retained ids start at `oldest`, so we need last_event_id >= oldest - 1.
        return oldest is not None and last_event_id >= oldest - 1

    def messages_after(self, last_event_id: int) -> list[dict[str, Any]]:
        """Retained stamped messages with seq > `last_event_id`, in order."""
        return [m for s, m in self._ring if s > last_event_id]
