# Downloading the dataset

The dataset package is hosted on Hugging Face:

**<https://huggingface.co/datasets/Mozzzy/SO-RoadUAV>**

It is not tracked by git. `scripts/download_dataset.py` writes it to `dataset/`
by default, which is the path every command in this repository expects.

## Quick start

```bash
pip install -r requirements.txt
python scripts/download_dataset.py
```

Full package: 74,509 files, about 5.5 GB.

## Partial downloads

| `--parts` | Contents | Size |
|---|---|---:|
| `all` (default) | everything | ~5.5 GB |
| `test` | `test/` + manifests + text artifacts | ~1.5 GB |
| `test-eval` | test masks, lane JSON, metadata, manifests | ~230 MB |
| `val` | `val/` + manifests + text artifacts | ~0.5 GB |
| `train` | `train/` + manifests + text artifacts | ~3.7 GB |

`test-eval` is enough to run the official scorer, because scoring needs the
ground-truth masks, lane polylines and metadata, but not the RGB frames:

```bash
python scripts/download_dataset.py --parts test-eval --verify
```

Downloads are resumable: re-running the same command continues where it stopped.

## Verification

`SHA256SUMS` covers all 74,508 payload and text files (it excludes itself):

```bash
python scripts/download_dataset.py --verify          # streaming check, no extra disk
cd dataset && sha256sum -c SHA256SUMS                # POSIX / Git Bash
```

On Windows without `sha256sum`, use `--verify` or run the check from Git Bash.
For a partial download, `--verify` checks the files that are present and reports
the rest as "not downloaded"; for a full download, missing files are an error.

## Using the Hugging Face CLI instead

```bash
pip install huggingface_hub
hf download Mozzzy/SO-RoadUAV --repo-type dataset --local-dir dataset
```

Scoring-only subset:

```bash
hf download Mozzzy/SO-RoadUAV --repo-type dataset --local-dir dataset \
    --include "test/lane_polyline_projected/*" "test/metadata/*" \
              "test/road_mask_visible/*" "manifests/*" "SHA256SUMS" \
              "LICENSE" "README.md" "dataset_card.json" "package_manifest.json"
```

## Expected layout

```
dataset/
├── README.md
├── LICENSE
├── dataset_card.json
├── package_manifest.json
├── SHA256SUMS
├── manifests/
│   ├── subsets/{train,val,test}.jsonl
│   └── views/{core_scene,core_observation,core_axis,challenge_l18}.jsonl
└── train|val|test/
    ├── rgb/<sample_id>.jpg
    ├── road_mask_visible/<sample_id>.png
    ├── lane_polyline_projected/<sample_id>.json
    └── metadata/<sample_id>.json
```

`huggingface_hub` keeps resumable-download metadata under `dataset/.cache/`.
It is ignored by git; pass `--clean-metadata` to remove it after a successful
full download.

## Offline and mirrored copies

The package is a plain directory tree, so it can be mirrored with `rsync`,
`rclone` or `git clone` from the Hugging Face repository. Every path in the
manifests is relative to the package root, so a mirrored copy works as-is as
long as the layout above is preserved.
