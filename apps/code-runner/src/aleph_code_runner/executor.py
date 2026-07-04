"""The execution contract for the sandbox (WP-4c).

`run_agent_code` runs agent-written Python in a **child subprocess** — never in
this process — with a wall-clock timeout and OS resource caps, capturing a
single declared output artifact from a fixed scratch path. It returns a plain
dict so it round-trips cleanly over arq's job result.

Isolation layering (see also the compose service + Dockerfile):

  1. Container level — `aleph-code-runner` sits on a Redis-only *internal*
     compose network. It can reach Redis (to pull jobs) but has no route to the
     internet, Postgres, or aleph-api, and carries no secrets in its env.
  2. Subprocess level — the child runs under ``python -I`` (isolated mode:
     ignores env vars + user site) and, before executing agent code, installs a
     socket guard that makes every network primitive raise, and best-effort
     unshares a fresh (empty) network namespace. It sets RLIMIT_CPU and
     RLIMIT_FSIZE. Process/thread count and memory are bounded by the container
     (pids_limit + mem_limit).

Residual: a determined escape via raw ctypes syscalls could bypass the
Python-level socket guard, but the container's internal network only reaches
Redis (which holds code-job payloads, no secrets), the rootfs is read-only, and
all capabilities are dropped — so the blast radius is CPU/mem within the caps.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal, TypedDict

# arq queue name shared with the privileged enqueuer in aleph-workers. Hardcoded
# (not imported) because this package must not depend on any aleph module.
CODE_RUNNER_QUEUE = "arq:queue:code_runner"

OutputKind = Literal["png", "svg", "vega", "html"]

_EXT: dict[str, str] = {"png": "png", "svg": "svg", "vega": "json", "html": "html"}
_MIME: dict[str, str] = {
    "png": "image/png",
    "svg": "image/svg+xml",
    "vega": "application/json",
    "html": "text/html; charset=utf-8",
}

# Defaults; the caller may lower them per job.
DEFAULT_TIMEOUT_S = 30
DEFAULT_CPU_S = 25
DEFAULT_FSIZE_BYTES = 32 * 1024 * 1024  # 32 MiB output cap
MAX_OUTPUT_BYTES = 24 * 1024 * 1024


class RunResult(TypedDict, total=False):
    ok: bool
    mime: str
    output_kind: str
    bytes_b64: str
    spec_json: dict[str, object]
    error: str


# The child harness. Self-contained: stdlib + the audited sci stack only (no
# local imports — under `python -I` the script dir is NOT on sys.path). It caps
# resources, denies network, execs the agent code, then materializes the single
# declared output to OUTPUT_PATH.
_HARNESS = r"""
import builtins, json, os, sys

_code_path, _output_kind, _output_path, _cpu_s, _fsize = sys.argv[1:6]
_cpu_s = int(_cpu_s); _fsize = int(_fsize)


def _lock_down():
    # (a) resource caps
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (_cpu_s, _cpu_s + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (_fsize, _fsize))
        # NB: RLIMIT_NPROC is per-real-UID system-wide, so we do NOT cap it here
        # (it would count unrelated host processes). The container's pids_limit
        # bounds process/thread count instead.
    except Exception:
        pass
    # (b) best-effort empty network namespace (needs privilege we don't have
    #     under cap_drop:ALL — harmless if it fails; the socket guard is the
    #     reliable layer).
    try:
        os.unshare(os.CLONE_NEWNET)  # type: ignore[attr-defined]
    except Exception:
        pass
    # (c) socket guard — make every network primitive fail closed with a clear
    #     OSError. `socket.socket` is replaced by a class whose constructor
    #     raises, so subclassing / isinstance machinery stays well-formed.
    import socket

    _MSG = "network access is disabled in the code_runner sandbox"

    class _NoNetSocket:
        def __init__(self, *_a, **_k):
            raise OSError(_MSG)

    def _blocked(*_a, **_k):
        raise OSError(_MSG)

    socket.socket = _NoNetSocket      # type: ignore[assignment,misc]
    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.create_server = _blocked   # type: ignore[assignment]
    socket.getaddrinfo = _blocked     # type: ignore[assignment]
    socket.gethostbyname = _blocked   # type: ignore[assignment]


def _materialize(ns):
    kind = _output_kind
    out = _output_path
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return
    if kind in ("png", "svg"):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        if plt.get_fignums():
            plt.savefig(out, format=kind, bbox_inches="tight")
            return
        raise RuntimeError("no matplotlib figure produced and OUTPUT_PATH not written")
    if kind == "vega":
        spec = ns.get("spec") or ns.get("chart")
        if spec is None:
            raise RuntimeError("vega output needs a top-level `spec` dict or a written OUTPUT_PATH")
        # altair Chart -> dict
        to_dict = getattr(spec, "to_dict", None)
        if callable(to_dict):
            spec = to_dict()
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(spec, fh)
        return
    if kind == "html":
        html = ns.get("html")
        if not isinstance(html, str):
            raise RuntimeError("html output needs a top-level `html` str or a written OUTPUT_PATH")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(html)
        return
    raise RuntimeError("unknown output kind: %r" % kind)


def main():
    _lock_down()
    builtins.OUTPUT_PATH = _output_path  # type: ignore[attr-defined]
    with open(_code_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    ns: dict = {"__name__": "__aleph_agent_code__", "OUTPUT_PATH": _output_path}
    exec(compile(src, "<agent_code>", "exec"), ns)
    _materialize(ns)


main()
"""


def run_agent_code(
    code: str,
    output_kind: OutputKind,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    cpu_s: int = DEFAULT_CPU_S,
    fsize_bytes: int = DEFAULT_FSIZE_BYTES,
    scratch_root: str | None = None,
) -> RunResult:
    """Execute `code` in a locked-down subprocess and capture its single output.

    Returns ``{ok: True, ...}`` with either ``bytes_b64`` (png/svg/html) or
    ``spec_json`` (vega), or ``{ok: False, error: ...}`` on any failure
    (timeout, nonzero exit, missing output). Never raises for agent-code faults.
    """
    if output_kind not in _EXT:
        return {"ok": False, "error": f"unsupported output_kind: {output_kind!r}"}
    ext = _EXT[output_kind]
    with tempfile.TemporaryDirectory(dir=scratch_root, prefix="coderun-") as scratch:
        d = Path(scratch)
        code_path = d / "agent_code.py"
        harness_path = d / "_harness.py"
        out_path = d / f"output.{ext}"
        code_path.write_text(code, encoding="utf-8")
        harness_path.write_text(_HARNESS, encoding="utf-8")
        # `-I` isolates the interpreter from env vars + user site-packages. The
        # child inherits only the args below; no credentials are ever passed.
        argv = [
            sys.executable,
            "-I",
            str(harness_path),
            str(code_path),
            output_kind,
            str(out_path),
            str(cpu_s),
            str(fsize_bytes),
        ]
        try:
            proc = subprocess.run(
                argv,
                cwd=scratch,
                capture_output=True,
                timeout=timeout_s,
                env={
                    "PATH": "/usr/bin:/bin",
                    "MPLBACKEND": "Agg",
                    "HOME": scratch,
                    "MPLCONFIGDIR": scratch,
                    # Keep native BLAS/OpenMP single-threaded: deterministic,
                    # light, and it avoids thread-spawn storms under the caps.
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                    "NUMEXPR_NUM_THREADS": "1",
                },
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"execution timed out after {timeout_s}s"}
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace")[-2000:]
            return {"ok": False, "error": f"agent code failed: {tail}"}
        if not out_path.exists() or out_path.stat().st_size == 0:
            return {"ok": False, "error": "no output artifact produced"}
        if out_path.stat().st_size > MAX_OUTPUT_BYTES:
            return {"ok": False, "error": "output artifact exceeds size cap"}
        raw = out_path.read_bytes()
        if output_kind == "vega":
            try:
                spec = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                return {"ok": False, "error": f"vega output is not valid JSON: {exc}"}
            return {"ok": True, "output_kind": "vega", "mime": _MIME["vega"], "spec_json": spec}
        return {
            "ok": True,
            "output_kind": output_kind,
            "mime": _MIME[output_kind],
            "bytes_b64": base64.b64encode(raw).decode("ascii"),
        }
