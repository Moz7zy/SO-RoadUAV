# SO-RoadUAV

[![Dataset: Hugging Face](https://img.shields.io/badge/dataset-Hugging%20Face-yellow)](https://huggingface.co/datasets/Mozzzy/SO-RoadUAV)
[![Code: MIT](https://img.shields.io/badge/code-MIT-blue)](LICENSE)
[![Data: CC BY-NC 4.0](https://img.shields.io/badge/data-CC%20BY--NC%204.0-lightgrey)](LICENSE-DATASET)

SO-RoadUAV is a CARLA-first benchmark for **map-assisted low-altitude road
inspection from single-frame UAV RGB images**. It publishes two supervised
targets only:

- `road_mask_visible` — the CARLA semantic `Roads=1 + RoadLines=24` mask of the
  rendered frame.
- `lane_polyline_projected` — image-space lane-marking polylines inside the
  target corridor, with `projected_in_image_no_occlusion_test` semantics.

The release contains 18,624 images (12,000 train / 1,584 val / 5,040 test) from
three CARLA towns, together with the generator that produced them and a
model-agnostic scorer.

Status: **released**. Code is licensed under the MIT License; the dataset is
licensed under CC BY-NC 4.0 (Attribution-NonCommercial). See [`LICENSE`](LICENSE)
and [`LICENSE-DATASET`](LICENSE-DATASET).

**Naming.** The benchmark is referred to as *Benchmark V3* (and `benchmark_v3` in
code identifiers and historical artifacts) throughout its development. Its public
name is **SO-RoadUAV**; both names denote the same dataset and the same protocol
version.

## Data

The 5.5 GB dataset package (18,624 images) is hosted on Hugging Face:

**<https://huggingface.co/datasets/Mozzzy/SO-RoadUAV>**

```bash
pip install -r requirements.txt
python scripts/download_dataset.py --dest dataset
```

Scoring needs only the test split: `--parts test-eval` downloads ~230 MB (road
masks, lane JSON, metadata, manifests) instead of the full package. See
[`docs/DOWNLOAD.md`](docs/DOWNLOAD.md) for partial downloads and verification.

## Repository layout

| Path | Contents |
|---|---|
| `dataset/` | download target for the dataset package (not tracked by git); see [Data](#data) |
| `scripts/` | `download_dataset.py`, the Hugging Face downloader and checksum verifier |
| `generation/` | the CARLA generator: plan, capture worker, map preparation, dataset packaging |
| `evaluation/` | the official metrics and `score_predictions.py` |
| `docs/` | protocol, download, generation, evaluation, baseline adaptations, reproducibility, provenance |

Start with [`docs/DATASET.md`](docs/DATASET.md) for the label contract and
manifest structure.

## Dataset at a glance

| Subset | Images | Sites | Anchors |
|---|---:|---:|---:|
| train | 12,000 | 131 | 250 |
| val | 1,584 | 20 | 33 |
| test | 5,040 | 53 | 126 |

Four logical views index the test split: `core_scene` (5,040),
`core_observation` (1,890), `core_axis` (882) and `challenge_l18` (2,268).

Observation space: altitude 20–100 m, pitch −90°…−70°, yaw −15°…+15°, traffic
0–40 vehicles/lane/km, weather ∈ {dry, rain-wet, fog}, sun altitude −5°…75°.

After downloading, verify integrity from `dataset/`:

```bash
cd dataset && sha256sum -c SHA256SUMS
```

## Scoring

```bash
python evaluation/score_predictions.py \
    --dataset dataset \
    --predictions ./preds \
    --model my_model \
    --out ./scores
```

Predictions are `preds/road/<sample_id>.png` (binary 0/255, 1280×720) and
`preds/lane/<sample_id>.json` (`{"instances": [{"points_image": [[x, y], ...]}]}`,
at most 10 instances). The scorer writes per-sample metrics and a view-level
report with site-macro aggregates, 95% bootstrap intervals and reference-relative
deltas. See [`docs/EVALUATION.md`](docs/EVALUATION.md).

## Reproducing the dataset

```bash
python generation/generate_dataset.py --out /data/Benchmark-V3 --plan-only
python generation/generate_dataset.py --out /data/Benchmark-V3 \
    --carla-python /path/to/python --carla-root /opt/Carla --gpu 0
```

`--plan-only` is fully offline and reproduces the frozen 18,624-row plan
byte-for-byte. Full capture requires a CARLA server and the three OpenDRIVE maps
shipped under `generation/inputs/maps/`. See
[`docs/GENERATION.md`](docs/GENERATION.md) and
[`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md).

## Baseline notes

This repository does **not** ship baseline model code, weights or result tables.
[`docs/BASELINE_ADAPTATIONS.md`](docs/BASELINE_ADAPTATIONS.md) records the
adaptations needed to train six reference models (SegFormer-B0,
Mask2Former-R50, YOLO26s-semantic, LaneATT-R18, CondLaneNet-R18, CLRNet-R18) on
this benchmark.

## Data availability

The dataset is publicly available at
<https://huggingface.co/datasets/Mozzzy/SO-RoadUAV> under CC BY-NC 4.0. The
generation pipeline and official scorer are released at
<https://github.com/Moz7zy/SO-RoadUAV> under the MIT License.

## License and citation

Code (`generation/`, `evaluation/`, `scripts/`) is released under the MIT
License. The dataset is released under **CC BY-NC 4.0**: reuse and adaptation
are permitted with attribution, commercial use is not. See
[`LICENSE-DATASET`](LICENSE-DATASET) for the full notice.

The RGB images are renderings of CARLA map assets. The CARLA project distributes
its specific assets under CC-BY (CVC/UAB); those assets remain available under
CC-BY and are not restricted by the NonCommercial term, which applies to the
SO-RoadUAV curation and annotations. See [`NOTICE`](NOTICE).

Please cite:

> Wang, Z., Zhao, J., Liu, X., Gao, X., & Ye, K. *SO-RoadUAV: A Scene-Observation
> Diagnostic Benchmark for UAV Traffic-Monitoring Road Perception* (2026).

Machine-readable metadata is in [`CITATION.cff`](CITATION.cff).
