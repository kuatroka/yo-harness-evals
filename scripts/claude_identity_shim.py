"""Strip the Claude Code text that Devin's content policy rejects.

Devin's Cascade API answers `permission_denied: ... blocked by our content
policy` for three pieces of Claude Code's own prompt:

1. the identity line `You are a Claude agent, built on Anthropic's Claude Agent SDK.`
2. the safety paragraph beginning `IMPORTANT: Assist with authorized security testing`
3. the vendor paragraph beginning `The most recent Claude models are the Claude`

Everything else passes, including tools, thinking, metadata and the task
prompt. This proxy sits between Claude Code and 9router, removes those
paragraphs, and streams the response back unchanged.

Point Claude Code at this proxy with ``ANTHROPIC_BASE_URL=http://host:20140``
and no ``/v1`` suffix, since the client appends ``/v1/messages`` itself.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DENY_MARKERS = (
    "built on Anthropic's Claude Agent SDK",
    "built on Anthropic\u2019s Claude Agent SDK",
    "IMPORTANT: Assist with authorized security testing",
    "The most recent Claude models are the Claude",
)


def _sanitize_text(text: str) -> str | None:
    """Drop the offending paragraphs from one block of system text."""
    kept = [
        paragraph
        for paragraph in text.split("\n\n")
        if not any(marker in paragraph for marker in DENY_MARKERS)
    ]
    joined = "\n\n".join(kept).strip()
    return joined or None


def sanitize_system(system: object) -> object:
    """Return the system prompt with the rejected paragraphs removed.

    Accepts either the string form or the block-list form and preserves every
    other block. Returns None when nothing is left, so the caller omits the
    ``system`` key rather than sending an empty list.
    """
    if system is None:
        return None
    if isinstance(system, str):
        return _sanitize_text(system)
    if not isinstance(system, list):
        return system

    kept = []
    for block in system:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            kept.append(block)
            continue
        text = _sanitize_text(block["text"])
        if text is not None:
            kept.append({**block, "text": text})
    return kept or None


def rewrite_body(raw: bytes) -> bytes:
    """Return the request body with the rejected system paragraphs removed."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(payload, dict) or "system" not in payload:
        return raw
    stripped = sanitize_system(payload["system"])
    if stripped is not None:
        payload["system"] = stripped
    else:
        payload.pop("system", None)
    return json.dumps(payload).encode()



class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream_host = "127.0.0.1"
    upstream_port = 20128

    def _upstream_path(self) -> str:
        path = self.path.split("?", 1)[0]
        if path.endswith("/messages"):
            return "/v1/messages" + ("?" + self.path.split("?", 1)[1] if "?" in self.path else "")
        return self.path

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length)
        body = rewrite_body(raw)

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in ("host", "content-length", "connection")
        }
        headers["content-length"] = str(len(body))

        connection = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=900
        )
        try:
            connection.request(
                "POST", self._upstream_path(), body=body, headers=headers
            )
            response = connection.getresponse()
        except Exception as error:  # noqa: BLE001
            self.send_response(502)
            self.send_header("content-type", "application/json")
            payload = json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": str(error)}}
            ).encode()
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() in ("transfer-encoding", "content-length", "connection"):
                continue
            self.send_header(key, value)
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        try:
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            connection.close()

    def log_message(self, *args) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=20140)
    parser.add_argument("--upstream-port", type=int, default=20128)
    parser.add_argument("--quiet", action="store_true")
    options = parser.parse_args()

    Proxy.upstream_port = options.upstream_port
    server = ThreadingHTTPServer((options.host, options.port), Proxy)
    server.daemon_threads = True
    if not options.quiet:
        print(
            f"claude identity shim on {options.host}:{options.port} -> "
            f"http://127.0.0.1:{options.upstream_port}/v1/messages",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
