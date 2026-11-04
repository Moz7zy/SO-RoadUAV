# Reproducibility

## 1. Environment

| Component | Reference value |
|---|---|
| CARLA server | 0.9.15 (`/opt/Carla/CarlaUE4.sh`) |
| CARLA Python client | an interpreter that can `import carla` |
| GPU | one idle adapter; the reference run used an RTX 3080 Ti, GPU index 6 |
| Ports | RPC 3110, Traffic Manager 8110 (both dedicated) |
| Maps | Town04_Opt, Town05_Opt, Town10HD_Opt |

The generator requires no SUMO, no pedestrians and no network access beyond the
local CARLA RPC port.

The evaluation package requires only `numpy`, `opencv-python` and `scipy`. It
must not require `torch`.

## 2. OpenDRIVE checksums

| Map | SHA-256 |
|---|---|
| `Town04_Opt.xodr` | `4dddd1560ab7812b0e89cd4a523d8b6f2b93947eafb496d9eeb83cc772116c85` |
| `Town05_Opt.xodr` | `7edc53b0be840e177598da7a4ed9571146d7b2595ce280d154c90f92e8a48851` |
| `Town10HD_Opt.xodr` | `5d883b799f634030af92be1e9d79d107845540ba04338e8c60e095be1aef7be7` |

These match the values recorded in `dataset/dataset_card.json`.

## 3. Planning is exactly reproducible

The published plan is frozen. `generate_dataset.py --plan-only` must reproduce
the 18,624-row ledger byte-for-byte:

```
expected_sample_ledger.jsonl
sha256 = 8226d90354ad302790def30ab41132dac9ae19ef85b179c2f65f057942eb0506
```

The public package ships the plan under public naming, so the shipped ledger
hash differs; reverting the five naming fields
(`protocol_version`, `label_rule_version`, `display_name`, `recipe_family`,
`source_uri`) reproduces the hash above exactly. This was verified for all
18,624 rows.

Sample identity is intentionally pinned: `make_sample_id` hashes the frozen
identity protocol string `V3-PLAN-20260815-FULL.3`, not the public
`Benchmark-V3-1.0` label. Changing the identity string would renumber every
sample and every file name.

## 4. Packaging is deterministic

`package_dataset.py` serialises a capture run into `dataset/` with a fixed JSON
style (`indent=2`, sorted keys, UTF-8) and no timestamp in the package cards.
Running it twice on the same run produces identical bytes.

The shipped `dataset/` is, by construction, exactly the output of
`package_dataset.py` applied to the reference capture run: all 74,496 payload
files are byte-identical, and the twelve text artifacts covered by `SHA256SUMS`
(two READMEs, two cards, the dataset `LICENSE`, seven manifests) plus
`SHA256SUMS` itself are the packager's output.

## 5. Reference run facts

| Fact | Value |
|---|---|
| Start | 2026-08-21T16:04:34Z |
| Finalise | 2026-08-21T22:21:25Z |
| Wall time | ≈ 6 h 17 min |
| Shards per map | 8 |
| Accepted | 18,624 / 18,624 |
| Retries | 6 transient actor-transform drifts |

Wall time is hardware dependent; the figures above are informational only.

## 6. Version statement

The public package is a re-serialisation of a frozen capture run. Relative to
that run:

- RGB and road-mask bytes are unchanged (verified against the frozen asset
  inventory: 37,248 assets, zero mismatches).
- Lane JSON and metadata differ only in naming fields; removing those fields
  reproduces the original documents exactly.
- `dataset_card.json`, `package_manifest.json` and `SHA256SUMS` are derived
  files, recomputed for the public naming contract.

The package is therefore the same data version under public naming, not a new
capture.

## 7. What is not included

- No baseline training code, checkpoints, logs or metric tables.
- No CARLA server binaries, no compiled maps.
- No generated QA contact sheets, overlays or videos.
