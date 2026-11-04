"""Short-lived CARLA readiness and one-map preparation for Benchmark-V3."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from benchmark_generator.config import ConfigError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Internal Benchmark-V3 map preparer")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--map-id", required=True)
    parser.add_argument("--startup-timeout", type=float, required=True)
    parser.add_argument("--map-timeout", type=float, required=True)
    return parser.parse_args(argv)


def _map_name(world: object) -> str:
    return str(world.get_map().name).split("/")[-1]  # type: ignore[attr-defined]


def prepare(args: argparse.Namespace) -> dict[str, object]:
    import carla  # type: ignore
    deadline = time.monotonic() + float(args.startup_timeout)
    client = None
    world = None
    last_error = None
    while time.monotonic() < deadline:
        try:
            client = carla.Client(args.host, int(args.port))
            client.set_timeout(min(5.0, max(1.0, deadline - time.monotonic())))
            world = client.get_world()
            break
        except BaseException as exc:
            last_error = exc
            time.sleep(1.0)
    if world is None or client is None:
        raise ConfigError(f"Benchmark-V3 CARLA readiness timeout: {last_error}")
    initial = _map_name(world)
    loaded = False
    if initial != args.map_id:
        client.set_timeout(float(args.map_timeout))
        world = client.load_world(args.map_id)
        loaded = True
    deadline = time.monotonic() + float(args.map_timeout)
    actual = _map_name(world)
    while actual != args.map_id and time.monotonic() < deadline:
        client.set_timeout(min(10.0, max(1.0, deadline - time.monotonic())))
        world = client.get_world()
        actual = _map_name(world)
        if actual != args.map_id:
            time.sleep(1.0)
    if actual != args.map_id:
        raise ConfigError(f"Benchmark-V3 map mismatch: expected {args.map_id}, got {actual}")
    return {"status": "ready", "map_id": actual, "initial_map": initial, "map_load_performed": loaded}


def main(argv: list[str] | None = None) -> int:
    try:
        result = prepare(parse_args(argv))
    except BaseException as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    os._exit(main())
