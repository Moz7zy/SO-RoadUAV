#!/usr/bin/env python3
"""Benchmark-V3 dataset generator.

One entry point for the full pipeline:

    plan  ->  CARLA capture  ->  public dataset package

``--plan-only`` stops after the plan and never imports or contacts CARLA.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

GENERATION_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = GENERATION_ROOT / "configs" / "benchmark_v3.json"
MAP_IDS = ("Town04_Opt", "Town05_Opt", "Town10HD_Opt")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark-V3 dataset generator")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, required=True, help="output root")
    parser.add_argument("--plan-only", action="store_true", help="build the frozen plan and stop")
    parser.add_argument("--start-map", choices=MAP_IDS)
    parser.add_argument("--end-map", choices=MAP_IDS)
    parser.add_argument("--resume", type=Path, help="resume an existing capture run")
    parser.add_argument("--carla-python", default=sys.executable, help="Python interpreter that imports carla")
    parser.add_argument("--carla-root", default="/opt/Carla")
    parser.add_argument("--carla-executable", default=str(Path("/opt/Carla/CarlaUE4.sh")))
    parser.add_argument("--gpu", type=int, default=0, help="graphics adapter index")
    parser.add_argument("--port", type=int, default=3110, help="CARLA RPC port")
    parser.add_argument("--tm-port", type=int, default=8110, help="Traffic Manager port")
    parser.add_argument("--shard-count", type=int, default=8)
    parser.add_argument("--skip-package", action="store_true", help="capture only; do not build the dataset package")
    parser.add_argument("--package-timestamp", help="optional fixed timestamp recorded in the package report")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def bootstrap(output_root: Path) -> Path:
    output_root = output_root.expanduser().resolve()
    # Must be set before any generator submodule is imported.
    os.environ["BENCHMARK_V3_OUTPUT_ROOT"] = str(output_root)
    if str(GENERATION_ROOT) not in sys.path:
        sys.path.insert(0, str(GENERATION_ROOT))
    output_root.mkdir(parents=True, exist_ok=True)
    return output_root


def latest_plan_run(plan_root: Path) -> Path:
    runs = sorted(path for path in plan_root.glob("run_*") if (path / "static_manifest.json").is_file())
    if not runs:
        raise SystemExit(f"no plan run under {plan_root}; run without --plan-only first")
    return runs[-1]


def gpu_preflight(adapter: int) -> dict[str, Any]:
    query = ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]
    completed = subprocess.run(query, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"nvidia-smi failed: {completed.stderr.strip()}")
    rows = []
    for line in completed.stdout.splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) >= 5:
            rows.append({"index": int(fields[0]), "name": fields[1], "utilization_gpu": int(fields[2]),
                         "memory_used_mib": int(fields[3]), "memory_total_mib": int(fields[4])})
    selected = next((row for row in rows if row["index"] == adapter), None)
    if selected is None:
        raise SystemExit(f"graphics adapter {adapter} is not visible")
    if selected["utilization_gpu"] > 20 or selected["memory_used_mib"] > 1024:
        raise SystemExit(f"graphics adapter {adapter} is not idle: {selected}")
    return {"graphics_adapter": adapter, "selected": selected, "all_gpus": rows,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "query": query}


def apply_overrides(bundle: dict[str, Any], args: argparse.Namespace) -> None:
    config = bundle["config"]
    execution = bundle["execution_config"]
    for target in (config, execution):
        target["lifecycle"].update(
            {"host": "localhost", "rpc_port": args.port, "traffic_manager_port": args.tm_port,
             "graphics_adapter": args.gpu}
        )
        target["runtime"].update(
            {"carla_python": args.carla_python, "carla_root": args.carla_root,
             "carla_executable": args.carla_executable}
        )
        if "sharding" in target:
            target["sharding"]["shard_count"] = args.shard_count


def worker_command(context: dict[str, Any], map_id: str, shard: int, result_path: Path, args: argparse.Namespace) -> list[str]:
    return [context["config"]["runtime"]["carla_python"], "-B", "-m", "benchmark_generator.carla_client.worker",
            "--static-run", str(context["static_run"]), "--run-dir", str(context["run_dir"]),
            "--run-id", context["run_id"], "--map-id", map_id, "--shard-index", str(shard),
            "--shard-count", str(context["config"]["sharding"]["shard_count"]),
            "--result-path", str(result_path), "--host", "localhost", "--port", str(args.port),
            "--tm-port", str(args.tm_port)]


def map_preparer_command(context: dict[str, Any], map_id: str, spec, args: argparse.Namespace) -> list[str]:
    return [context["config"]["runtime"]["carla_python"], "-B", "-m", "benchmark_generator.carla_client.map_preparer",
            "--host", "localhost", "--port", str(args.port), "--map-id", map_id,
            "--startup-timeout", str(spec.startup_timeout_seconds),
            "--map-timeout", str(spec.map_load_timeout_seconds)]


def expected_ids(context: dict[str, Any], map_id: str, shard: int) -> set[str]:
    shard_count = int(context["config"]["sharding"]["shard_count"])
    rows = sorted((row for row in context["rows"] if row["map_id"] == map_id), key=lambda item: item["sample_id"])
    return {row["sample_id"] for index, row in enumerate(rows) if index % shard_count == shard}


def next_result(run_dir: Path, map_id: str, shard: int, expected: set[str]) -> tuple[Path, dict[str, Any] | None]:
    workers = run_dir / "_workers"
    base = workers / f"{map_id}_shard{shard:02d}.json"
    candidates = [base, *sorted(workers.glob(f"{map_id}_shard{shard:02d}_resume*.json"))]
    existing = [path for path in candidates if path.is_file()]
    if existing:
        latest = existing[-1]
        result = json.loads(latest.read_text(encoding="utf-8"))
        classified = [set(result.get(name, [])) for name in ("accepted", "reused", "rejected")]
        disjoint = all(not (classified[i] & classified[j]) for i in range(3) for j in range(i + 1, 3))
        if (result.get("map_id") == map_id and int(result.get("shard_index", -1)) == shard
                and result.get("cleanup") == "pass" and set(result.get("expected", [])) == expected
                and disjoint and set().union(*classified) == expected):
            return latest, result
    return workers / f"{map_id}_shard{shard:02d}_resume{len(existing):02d}.json", None


def run_plan(output_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    from benchmark_generator.v3.plan import build_plan
    from benchmark_generator.v3.plan_artifacts import materialize_plan

    bundle = build_plan(args.config)
    result = materialize_plan(args.config)
    return {
        "mode": "plan_only",
        "rows": len(bundle["rows"]),
        "subset_counts": bundle["plan"]["subset_counts"],
        "view_counts": bundle["plan"]["view_counts"],
        "merge_sites": bundle["plan"]["merge_site_count"],
        "plan_run": result["run_dir"],
        "carla_imported": False,
    }


def select_maps(args: argparse.Namespace) -> list[str]:
    maps = list(MAP_IDS)
    if args.start_map:
        maps = maps[maps.index(args.start_map):]
    if args.end_map:
        maps = maps[: maps.index(args.end_map) + 1]
    return maps


def run_capture(output_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    from benchmark_generator.carla_client.lifecycle import CarlaLifecycleSpec, CarlaServerManager
    from benchmark_generator.common.io import append_jsonl, utc_now, write_json
    from benchmark_generator.config import ConfigError
    from benchmark_generator.v3.execution import (
        build_review_package,
        create_execution_context,
        finalize_execution,
        load_execution_bundle,
    )

    plan_run = latest_plan_run(output_root / "_work" / "plan")
    bundle = load_execution_bundle(plan_run)
    apply_overrides(bundle, args)
    gpu = gpu_preflight(args.gpu)
    context = create_execution_context(bundle, sys.argv, gpu, args.resume)
    run_dir = Path(context["run_dir"])
    append_jsonl(run_dir / "execution_breadcrumbs.jsonl",
                 {"event": "gpu_preflight_passed", "timestamp": utc_now(), "run_id": context["run_id"], "details": gpu})
    maps = select_maps(args)
    partial = maps != list(MAP_IDS)
    try:
        for map_index, map_id in enumerate(maps, start=1):
            map_rows = [row for row in context["rows"] if row["map_id"] == map_id]
            if not map_rows:
                continue
            attempt_id = f"{map_id}_{time.strftime('%Y%m%d_%H%M%S')}_{map_index:02d}"
            spec = CarlaLifecycleSpec.from_config(context["execution_config"], "localhost", False, map_id)
            manager = CarlaServerManager(spec, run_dir / "carla_logs" / f"{attempt_id}.log")
            map_error: BaseException | None = None
            cleanup_error: str | None = None
            try:
                append_jsonl(run_dir / "execution_breadcrumbs.jsonl",
                             {"event": "carla_server_start_requested", "timestamp": utc_now(),
                              "run_id": context["run_id"], "details": {"map_id": map_id, "command": manager.command, "gpu": args.gpu}})
                manager.start_or_adopt()
                if manager.adopted:
                    raise ConfigError("refusing to adopt an existing CARLA server")
                prepared = subprocess.run(map_preparer_command(context, map_id, spec, args), cwd=str(GENERATION_ROOT),
                                          capture_output=True, text=True,
                                          timeout=spec.startup_timeout_seconds + spec.map_load_timeout_seconds + 30, check=False)
                if prepared.returncode != 0:
                    raise ConfigError(f"map preparation failed {map_id}: {(prepared.stderr or prepared.stdout)[-4000:]}")
                for shard in range(int(context["config"]["sharding"]["shard_count"])):
                    expected = expected_ids(context, map_id, shard)
                    if not expected:
                        continue
                    result_path, reusable = next_result(run_dir, map_id, shard, expected)
                    if reusable is not None:
                        continue
                    completed = subprocess.run(worker_command(context, map_id, shard, result_path, args),
                                               cwd=str(GENERATION_ROOT), capture_output=True, text=True,
                                               timeout=float(context["config"]["limits"]["max_wall_seconds"]), check=False)
                    if completed.returncode != 0:
                        raise ConfigError(f"worker failed {map_id}/shard{shard}: {(completed.stderr or completed.stdout)[-4000:]}")
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if set(result.get("expected", [])) != expected or result.get("cleanup") != "pass":
                        raise ConfigError(f"worker result incomplete {map_id}/shard{shard}")
            except BaseException as exc:
                map_error = exc
            finally:
                try:
                    manager.stop()
                except BaseException as exc:
                    cleanup_error = f"{type(exc).__name__}: {exc}"
                append_jsonl(run_dir / "execution_breadcrumbs.jsonl",
                             {"event": "carla_server_stop_finished", "timestamp": utc_now(),
                              "run_id": context["run_id"],
                              "details": {"map_id": map_id, "result": "pass" if cleanup_error is None else "fail"}})
            if map_error is not None:
                raise map_error
            if cleanup_error is not None:
                raise ConfigError(f"CARLA cleanup failed {map_id}: {cleanup_error}")
        if partial:
            return {"mode": "partial_capture", "run_dir": str(run_dir), "maps": maps}
        final = finalize_execution(context)
        review = build_review_package(context)
        return {"mode": "capture", "run_dir": str(run_dir), **final, "review": review}
    except BaseException as exc:
        write_json(run_dir / "run_status.json",
                   {"run_id": context["run_id"], "status": "failed_resume_required",
                    "error_type": type(exc).__name__, "error_message": str(exc), "timestamp": utc_now()})
        raise


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_root = bootstrap(args.out)
    try:
        if args.plan_only:
            print(json.dumps(run_plan(output_root, args), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        capture = run_capture(output_root, args)
        payload: dict[str, Any] = {"capture": capture}
        if capture["mode"] == "capture" and not args.skip_package:
            from package_dataset import build

            report = build(
                Path(capture["run_dir"]),
                output_root / "dataset",
                output_root / "_work" / "package_report.json",
                args.package_timestamp,
            )
            payload["package"] = {
                "path": report["package_path"],
                "samples": report["directory_counts"]["samples"],
                "payload_files": report["payload_file_count"],
                "package_manifest_sha256": report["package_manifest_sha256"],
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
