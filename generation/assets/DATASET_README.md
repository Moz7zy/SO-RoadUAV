---
pretty_name: SO-RoadUAV
license: cc-by-nc-4.0
task_categories:
  - image-segmentation
tags:
  - carla
  - uav
  - aerial-imagery
  - benchmark
  - road-segmentation
  - lane-detection
  - autonomous-driving
  - synthetic-data
  - scene-observation
size_categories:
  - 10K<n<100K
---

# SO-RoadUAV Dataset (Benchmark-V3)

SO-RoadUAV is a CARLA-first benchmark for map-assisted low-altitude road
inspection from single-frame UAV RGB images. This directory is a deterministic
public serialisation of the frozen `Benchmark-V3-1.0` release.

Status: `released`.

- Code: <https://github.com/Moz7zy/SO-RoadUAV>
- Data: <https://huggingface.co/datasets/Mozzzy/SO-RoadUAV>

**Naming.** The benchmark is referred to as *Benchmark V3* (and `benchmark_v3` in
code identifiers and historical artifacts) throughout its development. Its public
name is **SO-RoadUAV**; both names denote the same dataset and the same protocol
version.

**License.** The dataset is released under **CC BY-NC 4.0**
(Attribution-NonCommercial 4.0 International): reuse and adaptation are permitted
with attribution, commercial use is not. See [`LICENSE`](LICENSE).

**Third-party assets.** The RGB images are renderings of CARLA map assets,
distributed by the CARLA project under CC-BY (CVC/UAB). The CC BY-NC 4.0
license applies to the SO-RoadUAV curation, annotations and packaging; it does
not restrict the CARLA map assets themselves.

## Distribution

The package is published as four tar archives under `archives/`, because the
Hub limits a Git-backed directory to 10,000 entries and `train/rgb`,
`train/metadata` and `train/lane_polyline_projected` each contain 12,000 files:

| Archive | Contents | Size |
|---|---|---:|
| `SO-RoadUAV-1.0-train.tar` | `train/` | 3.6 GB |
| `SO-RoadUAV-1.0-val.tar` | `val/` | 0.5 GB |
| `SO-RoadUAV-1.0-test.tar` | `test/` | 1.4 GB |
| `SO-RoadUAV-1.0-test-eval.tar` | `test/road_mask_visible`, `test/lane_polyline_projected`, `test/metadata` | 0.24 GB |

The archives preserve the directory layout below; extracting them into one
directory reproduces this package exactly. `archives/SHA256SUMS` covers the
archives, and `SHA256SUMS` covers the 74,508 files of the extracted package.

`python scripts/download_dataset.py --dest dataset` downloads, checks and
extracts the archives automatically. Manual extraction:

```bash
mkdir -p dataset
tar -xf SO-RoadUAV-1.0-train.tar -C dataset
tar -xf SO-RoadUAV-1.0-val.tar   -C dataset
tar -xf SO-RoadUAV-1.0-test.tar  -C dataset
```

The top-level text files (`README.md`, `LICENSE`, `SHA256SUMS`, `manifests/`,
the two JSON cards) are not inside the archives; download them alongside the
archives when assembling the package by hand.

## Tasks and labels

1. **Road-region segmentation** — target `road_mask_visible`, a binary PNG
   (0 / 255). Foreground is the CARLA semantic mask for `Roads=1` plus
   `RoadLines=24` in the rendered frame.
2. **Target-corridor lane recovery** — target `lane_polyline_projected`, a JSON
   document whose `instances[*].points_image` lists image-space lane-marking
   polylines with `projected_in_image_no_occlusion_test` semantics. Each
   instance is one distinct physical lane marking. The evaluation ROI is
   `target_corridor_polygon` from the sample metadata; predictions outside that
   ROI are not target-corridor evidence.

No paired Visible/Full targets are published. Road heading, corridor geometry,
lane graphs, topology tags and map priors are provided for slicing and QA only.

## Layout

```
train/  val/  test/
    rgb/                     JPEG, 1280 x 720
    road_mask_visible/       PNG, 0 / 255
    lane_polyline_projected/ JSON, at most 10 instances per image
    metadata/                cleaned per-sample metadata
manifests/
    subsets/{train,val,test}.jsonl
    views/{core_scene,core_observation,core_axis,challenge_l18}.jsonl
dataset_card.json
package_manifest.json
LICENSE
SHA256SUMS
```

Every sample appears exactly once under `train/`, `val/` or `test/`. The four
`views/` manifests are logical indexes over the same `test/` assets; they do not
duplicate images.

**Use the manifests as the entry point.** Do not construct the dataset by
scanning directories: the manifests carry the identity fields, subset routing
and slice membership that the evaluation protocol depends on.

Each subset manifest row resolves four relative asset paths (`rgb`,
`road_mask`, `lane`, `metadata`) against this directory, and repeats the
condition summary, reference linkage, factor labels and `label_rule_version`.

## Logical views

All four views are subsets of `test/`.

| View | Images | Meaning |
|---|---:|---|
| `core_scene` | 5,040 | every test image, for scene-level aggregation |
| `core_observation` | 1,890 | one-factor-at-a-time slice around each anchor reference |
| `core_axis` | 882 | seven stress axes |
| `challenge_l18` | 2,268 | multifactor challenge rows |

## Composition

| Subset | Images | Sites | Anchors |
|---|---:|---:|---:|
| train | 12,000 | 131 | 250 |
| val | 1,584 | 20 | 33 |
| test | 5,040 | 53 | 126 |

The test split covers 53 sites, 126 anchors and 25 leakage groups, with 40
unique images per anchor (41 logical slots including the reference alias).
Scenes: straight 1–4 and 5–8 lanes, curve 1–4 and 5–8 lanes, intersection and
ramp.

Observation ranges: height 20–100 m, pitch −90°…−70°, yaw −15°…+15°, traffic
0–40 vehicles/lane/km, three weather families (dry, rain-wet, fog) and sun
altitude −5°…75°.

## Label rules

Two label-rule versions are published:

| `label_rule_version` | Samples | Rule |
|---|---:|---|
| `lane_marking_projection_default_1.0` | 15,800 | OpenDRIVE marking projection with per-span de-duplication |
| `lane_marking_physical_merge_1.0` | 2,824 | physical cross-span marking merge at 24 confirmed sites |

`metadata.json` records which rule produced each sample. Lane instances are
already final: do not merge or split them downstream.

## Integrity

`SHA256SUMS` lists every payload file plus the two READMEs, both JSON cards, the
`LICENSE` and the seven manifests. Verify from this directory:

```bash
sha256sum -c SHA256SUMS
```

## Citation and license

License: **CC BY-NC 4.0** — see [`LICENSE`](LICENSE). Reuse and adaptation are
permitted with attribution; commercial use is not.

Citation:

> Wang, Z., Zhao, J., Liu, X., Gao, X., & Ye, K. *SO-RoadUAV: A
> Scene-Observation Diagnostic Benchmark for UAV Traffic-Monitoring Road
> Perception* (2026).

The images are rendered with the CARLA simulator from its map assets; consult the
CARLA project for the terms applicable to those assets.
