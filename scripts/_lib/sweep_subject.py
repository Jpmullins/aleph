"""A sweep must fail LOUDLY when the file it inspects is not there.

Every static sweep in `scripts/` names concrete subject files. When one of those
moves — a refactor, a rename, a package split — the sweep stops checking
anything. There are three ways that goes wrong, in increasing order of damage:

1. The sweep raises a raw ``FileNotFoundError`` from somewhere deep in its
   body. It exits nonzero, so CI is red, but the traceback names a line of the
   sweep rather than saying "your subject moved" — and the usual reaction is to
   delete the sweep.
2. The sweep's analyzer returns "no findings" for a file it never read, and a
   later read in the same script raises instead. That is
   `scripts/_lib/surface_bindings.run()` before this module existed: it returned
   ``[]`` when either subject was missing, and the nonzero exit came incidentally
   from the wrapper's *second* read of the same path. Reorder those two reads and
   the sweep passes green over nothing.
3. The sweep returns "no findings" and nothing else reads the file. It is then
   permanently green and permanently blind, which is strictly worse than not
   existing, because it occupies the slot where a working gate would go.

`MissingSubject` makes case 1 the only reachable outcome, with a message that
names the path and says what the sweep was going to do with it.

The same failure has been paid for at the mutation-testing layer:
`scripts/_acceptance/self_check.sh` had a probe naming a migration file a newer
migration had displaced, so it mutated a file the check no longer executed and
reported "can fail" having broken nothing. A missing subject is not a quiet
skip anywhere in this repo.
"""

from __future__ import annotations

import pathlib

__all__ = ["MissingSubject", "require_subject"]


class MissingSubject(FileNotFoundError):
    """A sweep's subject file is not where the sweep looks for it.

    Deliberately a `FileNotFoundError` subclass: a caller that forgets to catch
    it still crashes, rather than passing. Catching it only buys a better
    message, never a softer outcome.
    """


def require_subject(path: pathlib.Path, why: str) -> pathlib.Path:
    """Return `path`, or raise `MissingSubject` naming it and what it is for.

    `why` is prose for whoever moved the file: it should say what the sweep
    reads out of it, so the reader can decide whether to update the sweep's
    path or to move the check to wherever the code went.
    """
    if not path.is_file():
        msg = f"{path} is not there — {why}"
        raise MissingSubject(msg)
    return path
