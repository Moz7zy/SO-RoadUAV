# Generation pipeline

```
generation/
├── generate_dataset.py      one-shot CLI: plan -> capture -> package
├── package_dataset.py       run -> dataset/ packager (deterministic, CARLA-free)
├── benchmark_generator/
│   ├── config.py            path containment and JSON helpers
│   ├── common/              io, identity, cleanup
│   ├── labels/              frame-label assembly
│   ├── manifests/           run-manifest assembly
│   ├── qa/                  QA summary and contact-sheet layout
│   ├── validation/          offline artifact validators
│   ├── opendrive/           OpenDRIVE segment registry
│   ├── v2/geometry.py       target-corridor and lane-marking geometry
│   ├── v3/
│   │   ├── constants.py     roots, subsets, rule versions
│   │   ├── plan.py          planner (frozen 18,624-row ledger)
│   │   ├── plan_artifacts.py
│   │   ├── execution.py     capture context, finalisation, review package
│   │   ├── sampling.py      development sampling and traffic blueprints
│   │   ├── recipes.py       OFAT / L18 / axis recipe construction
│   │   ├── label_merge.py   cross-span physical-marking merge
│   │   ├── lane_instance_audit.py
│   │   ├── capture_plan.py  traffic layout and projection helpers
│   │   ├── capture_artifacts.py, capture_execution.py
│   │   ├── ids.py           stable identity derivation
│   │   ├── strict_geometry.py, replay.py, schema_support.py
│   │   └── opendrive.py
│   └── carla_client/
│       ├── worker.py        capture worker (sharded, never loads a map)
│       ├── map_preparer.py  isolated map preparation
│       ├── capture.py       per-sample CARLA capture
│       ├── carla_helpers.py sensor/actor/projection helpers
│       └── lifecycle.py     owned server process groups
├── configs/                 benchmark + engineering-base + L18/QA/height configs
├── schemas/v3/              JSON schemas used by the planner and worker
├── inputs/                  frozen planning inputs and OpenDRIVE maps
└── assets/                  dataset README templates used by the packager
```

Every module imports without CARLA and without a GPU; CARLA is only imported
inside the worker and map-preparer entry points.

## Quick start

```bash
# offline plan only (no CARLA import, no GPU)
python generate_dataset.py --out /data/Benchmark-V3 --plan-only

# full capture + packaging
python generate_dataset.py --out /data/Benchmark-V3 \
    --carla-python /path/to/carla/python \
    --carla-root /opt/Carla \
    --gpu 0 --port 3110 --tm-port 8110
```

`--plan-only` prints row counts and the plan directory. It writes
`<out>/_work/plan/run_<timestamp>/` with `expected_sample_ledger.jsonl`.

## Outputs

| Path | Contents |
|---|---|
| `<out>/_work/plan/run_<ts>/` | frozen ledger, manifests, label-rule scope, plan manifest |
| `<out>/_work/run/run_<ts>/` | capture run: `canonical/<sample_id>/`, QA, review, `run_manifest.json` |
| `<out>/dataset/` | public dataset package (what gets published) |
| `<out>/_work/package_report.json` | packaging gate report |

The published copy of `<out>/dataset/` is hosted on Hugging Face
(`Mozzzy/SO-RoadUAV`); see [`../docs/DOWNLOAD.md`](../docs/DOWNLOAD.md).

## Configuration

`configs/benchmark_v3.json` is the single entry config. It fixes:

- the 18,624-image composition and subset counts;
- the observation grid: height 20–100 m, pitch −90°…−70°, yaw −15°…+15°,
  traffic 0–40 vehicles/lane/km, three weather families, sun −5°…75°;
- development sampling (48 recipes per anchor, capacity-rank stratified,
  24 zero / 24 non-zero traffic);
- Core-16, L18 and axis definitions;
- the seven seeds;
- the 24-site label-merge scope;
- sharding (8) and retry budget (2 attempts).

`configs/engineering_base.json` supplies the behavioural parameters inherited
from the engineering base (camera, traffic layout, QA thresholds). It carries no
historical artifact paths and no selection pins.

`configs/challenge_l18.json`, `configs/qa_policies.json` and
`configs/height_calibration.json` are frozen leaf configs.

Paths inside configs are resolved relative to the package root, so the tree is
relocatable.

## Determinism

- Planning is pure: same inputs, same 18,624 rows, same sample ids.
- `package_dataset.py` writes JSON with `indent=2`, sorted keys, UTF-8, and no
  timestamps in the published cards. Re-running it on the same run produces
  identical bytes.

## Safety notes

- The worker never calls `load_world`; only the map preparer prepares a map.
- The wrapper owns exactly one CARLA process group per map and stops only that
  group. It refuses to adopt a pre-existing server.
- The plan is validated before any capture: if the ledger hash differs from the
  frozen expectation, capture does not start.
