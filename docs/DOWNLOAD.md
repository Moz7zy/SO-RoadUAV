# Downloading the dataset

The dataset package is hosted on Hugging Face:

**<https://huggingface.co/datasets/Mozzzy/SO-RoadUAV>**

Hugging Face limits a Git-backed directory to 10,000 entries. `train/rgb`,
`train/metadata` and `train/lane_polyline_projected` each contain 12,000 files,
so the package is published as four tar archives under `archives/`. Extracting
them reproduces the exact package layout; every path in the manifests stays
relative to the package root.

## Quick start

```bash
pip install -r requirements.txt
python scripts/download_dataset.py --dest dataset
```

The script downloads the archives, checks each archive against
`archives/SHA256SUMS`, extracts them into `dataset/`, and verifies the extracted
files against the package-level `SHA256SUMS`. Full package: 74,509 files, about
5.5 GB.

## Archives

| Archive | Contents | Size |
|---|---|---:|
| `SO-RoadUAV-1.0-train.tar` | `train/` | 3.6 GB |
| `SO-RoadUAV-1.0-val.tar` | `val/` | 0.5 GB |
| `SO-RoadUAV-1.0-test.tar` | `test/` | 1.4 GB |
| `SO-RoadUAV-1.0-test-eval.tar` | `test/` masks, lane JSON and metadata | 0.24 GB |

The top-level text artifacts (`README.md`, `LICENSE`, `README_CN.md`,
`dataset_card.json`, `package_manifest.json`, `SHA256SUMS`, `manifests/`) are
published outside the archives.

## Partial downloads

| `--parts` | Archives | Size |
|---|---|---:|
| `all` (default) | test + val + train | ~5.5 GB |
| `test` | test | ~1.4 GB |
| `test-eval` | test-eval | ~0.3 GB |
| `val` | val | ~0.5 GB |
| `train` | train | ~3.6 GB |

`test-eval` is enough to run the official scorer, because scoring needs the
ground-truth masks, lane polylines and metadata, but not the RGB frames:

```bash
python scripts/download_dataset.py --parts test-eval
```

Downloads are resumable: re-running the same command continues where it stopped.

## Manual download

```bash
pip install huggingface_hub
hf download Mozzzy/SO-RoadUAV --repo-type dataset --local-dir hf_pkg

mkdir -p dataset
tar -xf hf_pkg/archives/SO-RoadUAV-1.0-test.tar -C dataset
tar -xf hf_pkg/archives/SO-RoadUAV-1.0-val.tar  -C dataset
tar -xf hf_pkg/archives/SO-RoadUAV-1.0-train.tar -C dataset

cp -r hf_pkg/manifests hf_pkg/SHA256SUMS hf_pkg/LICENSE hf_pkg/README.md \
      hf_pkg/README_CN.md hf_pkg/dataset_card.json hf_pkg/package_manifest.json dataset/
```

## Verification

Two levels of checksums are published:

| File | Covers |
|---|---|
| `archives/SHA256SUMS` | the four tar archives |
| `SHA256SUMS` | the 74,508 files of the extracted package (it excludes itself) |

```bash
# archives
cd hf_pkg/archives && sha256sum -c SHA256SUMS

# extracted package (POSIX / Git Bash)
cd dataset && sha256sum -c SHA256SUMS
```

`download_dataset.py` performs both checks automatically. For a partial
download, it checks the files that are present and reports the rest as "not
downloaded"; for a full download, missing files are an error. On Windows without
`sha256sum`, use the script or run the check from Git Bash.

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
run.

## Offline and mirrored copies

The extracted package is a plain directory tree, so it can be mirrored with
`rsync`, `rclone` or a normal file copy. Every path in the manifests is relative
to the package root, so a mirrored copy works as-is as long as the layout above
is preserved. Keep the archives if you want a single-file backup of the
original bytes.
