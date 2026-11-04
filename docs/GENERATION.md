# Generation pipeline

## 1. Environment

| Component | Requirement |
|---|---|
| CARLA | 0.9.15 server, `/opt/Carla/CarlaUE4.sh` by default |
| Python | an interpreter that can `import carla` (`--carla-python`) |
| Maps | `Town04_Opt`, `Town05_Opt`, `Town10HD_Opt` OpenDRIVE files under `generation/inputs/maps/` |
| GPU | one idle graphics adapter (`--gpu`) |
| Ports | one dedicated RPC port and one Traffic Manager port (`--port`, `--tm-port`) |

No SUMO dependency. No pedestrians.

## 2. Two-phase design

The generator is deliberately split into a frozen plan and a capture run:

1. **Plan** — pure offline computation of all 18,624 rows: identity, requested
   conditions, traffic blueprint, expected projection. Written to
   `<out>/_work/plan/run_<timestamp>/` with a per-file checksum manifest.
2. **Capture** — one CARLA server process group per map, sharded workers, then
   finalisation and packaging.

Why two phases: the plan is auditable and diffable before any GPU time is spent,
and a capture run can resume against the exact same ledger.

`--plan-only` performs phase 1 and exits without importing CARLA.

## 3. Sampling design

### Development rows (train + val)

- 48 recipes per anchor.
- Capacity-rank stratified sampling with exponent 2.0 over the forward corridor.
- Traffic assignment: exactly 24 zero-traffic and 24 non-zero rows per anchor;
  the non-zero range is `(10, 40]` vehicles/lane/km.
- Ranges: height 20–100 m, pitch −90°…−70°, yaw −15°…+15°, sun altitude
  −5°…75°, weather ∈ {dry, rain-wet, fog}.

### Core slices (test)

- Core-16 bundle: height × pitch × yaw × traffic × weather × sun levels.
- 15 OFAT recipes per anchor (4 height, 2 pitch, 3 yaw, 2 traffic, 2 weather,
  2 illumination) plus the reference.
- 18 Challenge-L18 multifactor rows and 7 stress axes.

### Test-shared pool

53 sites / 126 anchors / 25 leakage groups, 40 unique images per anchor.
Development, validation and test sites are disjoint at the leakage-group level.

### Seeds

| Purpose | Seed |
|---|---:|
| development train | 2026081501 |
| development val | 2026081502 |
| challenge axis | 2026080804 |
| traffic master | 2026080105 |
| vehicle pool | 2026080106 |
| lane change | 2026080107 |
| review | 2026080908 |

## 4. Capture

Per map the wrapper:

1. starts an owned CARLA process group (never adopts an existing server);
2. runs the isolated map preparer once;
3. runs each shard worker, which never calls `load_world`;
4. stops exactly its own process group.

Sharding is `sorted_sample_id % shard_count` with 8 shards by default.
A sample may be attempted at most twice; transient actor-transform drift is the
only expected retry source.

## 5. Labels

- **Road mask** — CARLA semantic `Roads=1 + RoadLines=24`, written as 0/255 PNG.
- **Lane instances** — OpenDRIVE lane markings are projected into the image
  through the frozen camera pose, clipped to the target corridor, checked for
  actor occlusion (`projected_in_image_no_occlusion_test`), deduplicated, and
  optionally merged across spans at the 24 confirmed sites.

## 6. Commands

```bash
# offline plan only
python generation/generate_dataset.py --out /data/Benchmark-V3 --plan-only

# full capture + package
python generation/generate_dataset.py \
    --out /data/Benchmark-V3 \
    --carla-python /path/to/carla/python \
    --carla-root /opt/Carla \
    --gpu 0 --port 3110 --tm-port 8110 \
    --shard-count 8
```

Options: `--start-map` / `--end-map` for a bounded map range, `--resume` for an
existing run, `--skip-package` for capture only, `--package-timestamp` to pin the
report timestamp.

Outputs:

```
<out>/_work/plan/run_<ts>/     frozen plan
<out>/_work/run/run_<ts>/      capture run (canonical assets, QA, review)
<out>/dataset/                 public dataset package
<out>/_work/package_report.json
```

## 7. Failure handling

- A worker result is only accepted when its expected set is fully covered and
  cleanup is reported `pass`.
- A failed map aborts the run and leaves `run_status.json` at
  `failed_resume_required`; rerunning with `--resume` reuses complete shards.
- Incomplete staging bundles are preserved under `failed_attempts/`.

## 8. What the generator does not do

No training, no inference, no evaluation, no upload, no report generation.
