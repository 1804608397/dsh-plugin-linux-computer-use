"""Shared helpers: a tiny JSON-RPC client for the stdio server under test."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent / "server"
SERVER = SERVER_DIR / "linux_desktop_mcp.py"


class ServerGone(RuntimeError):
    """The server process died before answering (no session, no bus, ...)."""


class ServerGone(RuntimeError):
    """The server process died: a desktop service it needs is not available."""


class Server:
    """Spawn the real server and talk to it over stdio, exactly like DSH does."""

    def __init__(self, env: dict | None = None):
        self.home = tempfile.mkdtemp(prefix="cu-home-")
        environment = dict(os.environ)
        environment["HOME"] = self.home
        environment.pop("DSH_LINUX_CU_IDLE_GUARD_MS", None)
        environment.update(env or {})
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=str(SERVER_DIR),
            env=environment,
        )
        self._next_id = 0

    def send(self, method: str, params: dict | None = None) -> int:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        payload["params"] = params or {}
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError) as error:
            raise ServerGone(f"the server exited before the request could be sent: {error}") from error
        return self._next_id

    def wait(self, request_id: int) -> dict:
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise ServerGone("the server exited before answering")
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    def call(self, method: str, params: dict | None = None) -> dict:
        return self.wait(self.send(method, params))

    def initialize(self) -> dict:
        response = self.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "tests", "version": "0"},
            },
        )
        return response["result"]

    def tools(self) -> dict[str, dict]:
        listed = self.call("tools/list")["result"]["tools"]
        return {tool["name"]: tool for tool in listed}

    def tool(self, name: str, arguments: dict | None = None) -> tuple[bool, dict]:
        """Run one tool call; returns (is_error, first payload object)."""
        response = self.call("tools/call", {"name": name, "arguments": arguments or {}})
        result = response.get("result", {})
        blocks = result.get("content") or []
        payload: dict = {}
        for block in blocks:
            if block.get("type") == "text":
                try:
                    payload = json.loads(block["text"])
                except json.JSONDecodeError:
                    payload = {"text": block["text"]}
                break
        return bool(result.get("isError")), payload

    def error_text(self, name: str, arguments: dict | None = None) -> str:
        is_error, payload = self.tool(name, arguments)
        assert is_error, f"{name} was expected to fail, returned {payload}"
        return payload.get("text", "")

    def close(self) -> None:
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
