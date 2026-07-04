"""Execution-contract unit tests for the sandbox (WP-4c).

These exercise the real subprocess path (no container needed): the ok path
returns bytes for a trivial matplotlib PNG; agent code attempting network access
fails closed; a sleep is killed by the wall-clock timeout.
"""

from __future__ import annotations

import base64

from aleph_code_runner.executor import run_agent_code


def test_png_ok_returns_bytes() -> None:
    code = (
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3], [3, 1, 2])\n"
        "plt.title('t')\n"
    )
    result = run_agent_code(code, "png", timeout_s=30)
    assert result["ok"] is True, result
    assert result["mime"] == "image/png"
    raw = base64.b64decode(result["bytes_b64"])
    # PNG magic number.
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_vega_spec_ok_returns_json() -> None:
    code = "spec = {'mark': 'bar', 'data': {'values': [{'a': 1}]}}\n"
    result = run_agent_code(code, "vega", timeout_s=15)
    assert result["ok"] is True, result
    assert result["spec_json"]["mark"] == "bar"


def test_network_fails_closed() -> None:
    code = "import urllib.request\nurllib.request.urlopen('http://example.com', timeout=5)\n"
    result = run_agent_code(code, "png", timeout_s=15)
    assert result["ok"] is False
    assert "error" in result


def test_raw_socket_fails_closed() -> None:
    code = "import socket\ns = socket.socket()\n"
    result = run_agent_code(code, "png", timeout_s=15)
    assert result["ok"] is False


def test_timeout_kills_sleep() -> None:
    code = "import time\ntime.sleep(30)\n"
    result = run_agent_code(code, "png", timeout_s=2)
    assert result["ok"] is False
    assert "timed out" in result["error"]


def test_no_output_fails() -> None:
    code = "x = 1 + 1\n"
    result = run_agent_code(code, "png", timeout_s=15)
    assert result["ok"] is False
