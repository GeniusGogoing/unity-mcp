#!/usr/bin/env python3
"""Benchmark GameObject create RTT for HTTPLocal (default port 8092).

Modes (--via):
  rest  POST /api/command on MCP Server (shortest server path, bridge baseline)
  mcp   MCP call_tool over /mcp (Cursor-like, persistent session)
  cli   unity-mcp subprocess per call (includes process startup)

Run from Server venv:
  cd Server
  uv run python ../tools/benchmark_gameobject_rtt.py --via rest --port 8092 --cleanup
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

SERVER_DIR = Path(__file__).resolve().parents[1] / "Server"


def summarize(samples: list[float]) -> dict[str, float]:
    return {
        "median": statistics.median(samples),
        "mean": statistics.mean(samples),
        "min": min(samples),
        "max": max(samples),
        "stdev": statistics.stdev(samples) if len(samples) >= 2 else 0.0,
    }


def mcp_tool_ok(result: object) -> bool:
    if getattr(result, "isError", False):
        return False
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            data = json.loads(text)
            if data.get("success") is False:
                return False
            if data.get("status") == "error":
                return False
        except json.JSONDecodeError:
            pass
    return True


class RestApiClient:
    """HTTPLocal REST shortcut: POST /api/command (same path unity-mcp CLI uses)."""

    def __init__(self, port: int):
        if httpx is None:
            raise RuntimeError("httpx is required; run via: cd Server && uv run python ...")
        self.base_url = f"http://127.0.0.1:{port}"
        self.client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self.client.close()

    def send(self, command_type: str, params: dict) -> tuple[float, dict]:
        t0 = time.perf_counter()
        r = self.client.post(
            f"{self.base_url}/api/command",
            json={"type": command_type, "params": params},
        )
        r.raise_for_status()
        ms = (time.perf_counter() - t0) * 1000
        data = r.json()
        if data.get("status") == "error" or data.get("success") is False:
            raise RuntimeError(data.get("error") or data.get("message") or str(data))
        return ms, data

    def create_gameobject(self, name: str) -> tuple[float, dict]:
        return self.send(
            "manage_gameobject",
            {
                "action": "create",
                "name": name,
                "primitive_type": "Cube",
                "position": [0, 0, 0],
            },
        )

    def delete_gameobject(self, name: str) -> tuple[float, dict]:
        return self.send(
            "manage_gameobject",
            {
                "action": "delete",
                "target": name,
                "search_method": "by_name",
            },
        )

    def ping(self) -> tuple[float, dict]:
        return self.send("ping", {})


class UnityMcpClient:
    """Persistent MCP client over Streamable HTTP (/mcp)."""

    def __init__(self, port: int):
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.port = port
        self._session = None
        self.init_ms = 0.0

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["UnityMcpClient"]:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(self.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                t0 = time.perf_counter()
                await session.initialize()
                self.init_ms = (time.perf_counter() - t0) * 1000
                self._session = session
                try:
                    yield self
                finally:
                    self._session = None

    async def call_tool(self, name: str, arguments: dict) -> tuple[float, object]:
        assert self._session is not None
        t0 = time.perf_counter()
        result = await self._session.call_tool(name, arguments=arguments)
        ms = (time.perf_counter() - t0) * 1000
        if not mcp_tool_ok(result):
            raise RuntimeError(f"tool {name} failed: {result}")
        return ms, result

    async def create_gameobject(self, name: str) -> tuple[float, object]:
        return await self.call_tool(
            "manage_gameobject",
            {
                "action": "create",
                "name": name,
                "primitive_type": "Cube",
                "position": [0, 0, 0],
            },
        )

    async def delete_gameobject(self, name: str) -> tuple[float, object]:
        return await self.call_tool(
            "manage_gameobject",
            {
                "action": "delete",
                "target": name,
                "search_method": "by_name",
            },
        )


class UnityMcpCli:
    """Invoke unity-mcp CLI as a subprocess (new process per call)."""

    def __init__(self, port: int, server_dir: Path = SERVER_DIR):
        self.port = port
        self.server_dir = server_dir

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def run(self, args: list[str], timeout: float = 60.0) -> tuple[float, int, str, str]:
        cmd = ["uv", "run", "unity-mcp", "-p", str(self.port), *args]
        t0 = time.perf_counter()
        proc = subprocess.run(
            cmd,
            cwd=self.server_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._env(),
            encoding="utf-8",
            errors="replace",
        )
        return (time.perf_counter() - t0) * 1000, proc.returncode, proc.stdout, proc.stderr

    def create_gameobject(self, name: str) -> tuple[float, int, str, str]:
        return self.run(["gameobject", "create", name, "--primitive", "Cube"])

    def delete_gameobject(self, name: str) -> tuple[float, int, str, str]:
        return self.run(["gameobject", "delete", name, "--force"])


def print_summary(label: str, samples: list[float], extra: dict[str, str] | None = None) -> None:
    stats = summarize(samples)
    print("\n--- summary ---")
    print(f"via:        {label}")
    if extra:
        for k, v in extra.items():
            print(f"{k}: {v}")
    print(f"iterations: {len(samples)}")
    print(f"min:        {stats['min']:.1f} ms")
    print(f"max:        {stats['max']:.1f} ms")
    print(f"mean:       {stats['mean']:.1f} ms")
    print(f"median:     {stats['median']:.1f} ms")
    print(f"stdev:      {stats['stdev']:.1f} ms")


def run_rest_benchmark(port: int, iterations: int, warmup: int, prefix: str, cleanup: bool, profile: bool) -> int:
    client = RestApiClient(port)
    created: list[str] = []
    try:
        print("via:      HTTPLocal REST POST /api/command")
        print(f"endpoint: {client.base_url}")
        print(f"warmup={warmup}  iterations={iterations}\n")

        if profile:
            steps = [
                ("ping", lambda: client.ping()),
                ("create_go", None),
            ]
            all_samples: dict[str, list[float]] = {}
            for step, _ in steps:
                for _ in range(warmup):
                    if step == "create_go":
                        name = f"RTT_Rest_{len(created)}"
                        client.create_gameobject(name)
                        created.append(name)
                    else:
                        client.ping()
                samples: list[float] = []
                for i in range(iterations):
                    if step == "create_go":
                        name = f"RTT_Rest_{len(created)}"
                        ms, _ = client.create_gameobject(name)
                        created.append(name)
                    else:
                        ms, _ = client.ping()
                    samples.append(ms)
                    print(f"{step:18s} run {i + 1:02d}: {ms:7.1f} ms")
                all_samples[step] = samples
                stats = summarize(samples)
                print(f"{'':18s} median={stats['median']:7.1f} ms  mean={stats['mean']:7.1f} ms\n")

            ping_m = statistics.median(all_samples["ping"])
            create_m = statistics.median(all_samples["create_go"])
            print("=== breakdown (REST /api/command, persistent HTTP client) ===")
            print(f"ping median:          {ping_m:.1f} ms  (Unity bridge baseline)")
            print(f"create median:        {create_m:.1f} ms")
            print(f"create - ping:        {create_m - ping_m:.1f} ms  (tool delta)")
        else:
            for i in range(warmup):
                name = f"{prefix}_warmup_{i}"
                ms, _ = client.create_gameobject(name)
                print(f"warmup {i + 1}: {ms:7.1f} ms  ok=True")
                created.append(name)

            samples: list[float] = []
            for i in range(iterations):
                name = f"{prefix}_{i}"
                ms, _ = client.create_gameobject(name)
                samples.append(ms)
                created.append(name)
                print(f"run {i + 1:02d}: {ms:7.1f} ms  ok=True")
            print_summary("REST POST /api/command manage_gameobject create", samples)

        if cleanup and created:
            print("\ncleanup:")
            for name in created:
                try:
                    client.delete_gameobject(name)
                    print(f"  deleted {name}")
                except Exception as exc:
                    print(f"  failed to delete {name}: {exc}")

        print("\n=== path ===")
        print("httpx -> :8092/api/command -> PluginHub -> WebSocket -> Unity")
        return 0
    finally:
        client.close()


async def run_mcp_benchmark(port: int, iterations: int, warmup: int, prefix: str, cleanup: bool) -> int:
    client = UnityMcpClient(port)
    created: list[str] = []

    async with client.connect():
        print("via:        MCP Streamable HTTP")
        print(f"endpoint:   {client.url}")
        print(f"initialize: {client.init_ms:.1f} ms (one-time, not per call)")
        print(f"warmup={warmup}  iterations={iterations}\n")

        for i in range(warmup):
            name = f"{prefix}_warmup_{i}"
            ms, _ = await client.create_gameobject(name)
            print(f"warmup {i + 1}: {ms:7.1f} ms  ok=True")
            created.append(name)

        samples: list[float] = []
        for i in range(iterations):
            name = f"{prefix}_{i}"
            ms, _ = await client.create_gameobject(name)
            samples.append(ms)
            created.append(name)
            print(f"run {i + 1:02d}: {ms:7.1f} ms  ok=True")

        print_summary(
            "MCP call_tool manage_gameobject create",
            samples,
            {"initialize": f"{client.init_ms:.1f} ms (once)"},
        )

        if cleanup and created:
            print("\ncleanup:")
            for name in created:
                try:
                    await client.delete_gameobject(name)
                    print(f"  deleted {name}")
                except Exception as exc:
                    print(f"  failed to delete {name}: {exc}")

    print("\n=== path ===")
    print("MCP client -> :8092/mcp -> FastMCP Tool layer -> PluginHub -> WebSocket -> Unity")
    return 0


def run_cli_benchmark(port: int, server_dir: Path, iterations: int, warmup: int, prefix: str, cleanup: bool) -> int:
    cli = UnityMcpCli(port=port, server_dir=server_dir)
    created: list[str] = []
    samples: list[float] = []

    print("via:   unity-mcp CLI subprocess (new process per call)")
    print(f"port:  {port}\n")

    for i in range(warmup):
        name = f"{prefix}_warmup_{i}"
        ms, code, _, err = cli.create_gameobject(name)
        if code != 0:
            print(f"warmup failed: {err}", file=sys.stderr)
            return 2
        print(f"warmup {i + 1}: {ms:7.1f} ms  ok=True")
        created.append(name)

    for i in range(iterations):
        name = f"{prefix}_{i}"
        ms, code, _, err = cli.create_gameobject(name)
        if code != 0:
            print(f"run {i + 1:02d}: FAILED  {err}", file=sys.stderr)
            return 2
        samples.append(ms)
        created.append(name)
        print(f"run {i + 1:02d}: {ms:7.1f} ms  ok=True")

    if cleanup:
        print("\ncleanup:")
        for name in created:
            _, code, _, err = cli.delete_gameobject(name)
            if code == 0:
                print(f"  deleted {name}")
            else:
                print(f"  failed {name}: {err[:120]}")

    print_summary("unity-mcp CLI subprocess gameobject create", samples)
    print("\n=== path ===")
    print("subprocess(uv run unity-mcp) -> :8092/api/command -> PluginHub -> WebSocket -> Unity")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark GameObject create RTT (HTTPLocal)")
    parser.add_argument(
        "--via",
        choices=["rest", "mcp", "cli"],
        default="rest",
        help="rest=bridge baseline; mcp=Cursor path; cli=subprocess per call",
    )
    parser.add_argument("--port", type=int, default=8092, help="MCP Server HTTP port (HTTPLocal)")
    parser.add_argument("--server-dir", type=Path, default=SERVER_DIR, help="Server dir for uv run unity-mcp")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--prefix", default="RTT_Test")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--profile", action="store_true", help="Compare ping vs create (rest mode only)")
    args = parser.parse_args()

    if args.profile and args.via != "rest":
        print("--profile is only supported with --via rest", file=sys.stderr)
        return 1

    try:
        if args.via == "rest":
            return run_rest_benchmark(
                args.port, args.iterations, args.warmup, args.prefix, args.cleanup, args.profile
            )
        if args.via == "mcp":
            return asyncio.run(
                run_mcp_benchmark(args.port, args.iterations, args.warmup, args.prefix, args.cleanup)
            )
        return run_cli_benchmark(
            args.port, args.server_dir, args.iterations, args.warmup, args.prefix, args.cleanup
        )
    except subprocess.TimeoutExpired:
        print("Command timed out", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
