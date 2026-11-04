# Provenance and naming history

This file is the only place in the repository where internal iteration names are
documented. Everywhere else the public naming contract applies.

## 1. Dataset lineage

The published data comes from a full regeneration run (no asset reuse) that
superseded an earlier release with a narrower yaw range. Timeline:

| Date | Event |
|---|---|
| 2026-07-22 … 2026-07-28 | infrastructure, inventory, site review, pilot stages |
| 2026-07-31 | first restricted-scale capture accepted |
| 2026-08-01/02 | full-scale capture, post-exclusion derived dataset |
| 2026-08-08/09 | earlier public composition frozen; lane-instance label fix applied |
| 2026-08-15 | protocol revision: yaw range restored to ±15° |
| 2026-08-21/22 | full 18,624-image regeneration, review package, public package |
| 2026-10-01 | formal release freeze of the regenerated dataset |
| 2026-11-04 | this public package assembled |

The regenerated dataset supersedes the earlier ±10° yaw release. The earlier
composition is **not** part of this repository.

## 2. Field-level naming map

### Dataset fields

| Internal value | Public value |
|---|---|
| `V3-PLAN-20260815-FULL.3` | `Benchmark-V3-1.0` |
| `benchmark_v3_public_export_0.1` | `benchmark_v3_dataset_1.0` |
| `historical_v2_r4_project_then_full_polyline_dedup_default` | `lane_marking_projection_default_1.0` |
| `benchmark_v3_lane_physical_marking_cross_segment_merge_0.1` | `lane_marking_physical_merge_1.0` |
| `Benchmark-V3-Full.3` | `Benchmark-V3` |
| `benchmark_v3_full_3_public_dataset_pack_0.1` | `benchmark_v3_package_1.0` |
| `benchmark_v3_full_3_public_dataset_card_0.1` | `benchmark_v3_dataset_card_1.0` |
| `benchmark_v3_full_3_public_package_manifest_0.1` | `benchmark_v3_package_manifest_1.0` |

### Observation recipe families (visible inside `display_name`)

| Internal | Public |
|---|---|
| `full_3_development_stratified` | `development_stratified` |
| `full_3_shared_reference` | `shared_reference` |
| `full_3_ofat` | `ofat` |
| `full_3_l18` | `l18` |
| `full_3_axis` | `axis` |

### Modules

| Internal | Public |
|---|---|
| `full_3.py` | `plan.py` |
| `full_3_artifacts.py` | `plan_artifacts.py` |
| `full_3_execution.py` | `execution.py` |
| `r4.py` | `capture_plan.py` |
| `r4_artifacts.py` | `capture_artifacts.py` |
| `r4_execution.py` | `capture_execution.py` |
| `full_1.py` | `sampling.py` |
| `lane_physical_marking_merge.py` | `label_merge.py` |
| `full_3_worker.py` | `worker.py` |
| `full_3_map_preparer.py` | `map_preparer.py` |
| `r4_worker.py` | `capture.py` |
| `approved_devtest.py` | `carla_helpers.py` |
| `preview_artifacts.py` | removed (`file_sha256` moved to `common/io.py`) |
| `full_2.py` | removed (`SUBSETS` moved to `v3/constants.py`) |
| `e3_views.py` | `evaluation/metrics/aggregation.py` |
| `e3_statistics.py` | `evaluation/metrics/statistics.py` |

### Other identifiers

| Internal | Public |
|---|---|
| `r4` (capture stage) | `capture` |
| `p1_corrective_r4_closure_correction_v16_d070.json` | `configs/engineering_base.json` |
| `challenge_l18_rc_v2.json` | `configs/challenge_l18.json` |
| `qa_policies_ramp_v2_r4.json` | `configs/qa_policies.json` |
| `rc_height_calibration.json` | `configs/height_calibration.json` |
| `full_3_release_candidate.json` | `configs/benchmark_v3.json` |
| augmentation policy `V3-BASELINE-FULL2-SYNC-AUG.1` | referenced as `V3-BASELINE-SYNC-AUG.1` |
| CondLane attention patch `V3-CONDLANE-ATTENTION-OUT-OF-PLACE.1` | unchanged |

Internal schema-version strings that only appear inside `<out>/_work/` scratch
artifacts are retained as-is; they never reach `dataset/`.

## 3. Frozen planning inputs

`generation/inputs/` holds the frozen inputs the planner consumes. Each file is
pinned by `input_checksums.json`.

| File | Bytes | SHA-256 | Origin |
|---|---:|---|---|
| `site_registry.json` | 983,708 | `966622d2ad5c2e8e6767ed4a5c37b73c04ba8bf5e65e57aef785bb90ac689f57` | frozen finalization-v2 registry (309 sites) |
| `anchor_registry.json` | 431,998 | `437dd346ef150918b7a18e0ffe754ec0b709555f1bb2d2366ca9e6a988893162` | frozen finalization-v2 registry (423 anchors) |
| `road_library.json` | 4,243,240 | `ef2c6b2f45ecfefad8d292be3c4c994981c1711955758dff2a638b6a2f83a6e9` | frozen road library; OpenDRIVE paths rewritten to `inputs/maps/` |
| `split_decision.json` | 10,295 | `08f122e56a59a8f007cdcd81c184015156fc3d4dc90e75e611b20182a4b438dd` | frozen site split decision |
| `site_structural_cells.json` | — | `d1aca2bfb710c7e28f0f861368cbf9258892ab15eed1ac865612d5eca8f00f5c` | derived: per-site structural cell from the frozen planning ledger |
| `test_anchor_classification.json` | — | `56b4f66024fa7d1a0df3609f4bc0875cfed699c843d1a9a615d0c115d4fa5ffa` | derived: test-anchor yaw sign, original-core membership, frozen reference conditions |
| `maps/Town04_Opt.xodr` | 3,403,393 | `4dddd156…c85` | CARLA 0.9.15 map cache |
| `maps/Town05_Opt.xodr` | 4,651,162 | `7edc53b0…851` | CARLA 0.9.15 map cache |
| `maps/Town10HD_Opt.xodr` | 1,857,682 | `5d883b79…be7` | CARLA 0.9.15 map cache |

Two derived files deserve a note:

- `site_structural_cells.json` exists because the finalization-v2 site registry
  predates a scene reclassification: 91 of 204 sites carry a different
  `structural_cell` in the registry than the value the frozen plan used.
  Freezing the plan value is required for exact reproduction.
- `test_anchor_classification.json` exists because two of the frozen reference
  conditions differ only in the sign of zero (`yaw_relative_deg` is `0.0` for
  93 anchors and `-0.0` for 33), and that float sign is part of the recipe hash.
  The reference conditions themselves are otherwise identical for all 126
  anchors.

## 4. Statement

The public dataset is a re-serialisation of the frozen release under public
naming. It is not a new data version:

- RGB and road-mask bytes are unchanged.
- Lane instances and metadata content are unchanged.
- Only naming fields, package cards and manifests were rewritten.

The internal release identifiers are intentionally not reproduced here; the
frozen capture run identifier is `benchmark_v3_full_3_20260821_160430`.

## 5. Residual internal identifiers

Five internal tokens remain in `generation/benchmark_generator/v3/plan.py` and
nowhere else. They are not cosmetic leftovers; each one is load-bearing:

| Token | Why it must stay |
|---|---|
| `RULE_VERSION = "benchmark_v3_full_3_planning_0.1"` | feeds recipe ids, world/sensor seeds and therefore every sample id and file name |
| `make_recipe_id("full3", ...)` | frozen hash namespace for recipe ids |
| `pilot_kind: "full_3_full_regeneration"` | internal ledger field, stripped from published metadata |
| `full_3_status: "planned_pending_execution"` | internal ledger field, stripped from published metadata |
| `IDENTITY_PROTOCOL_VERSION = "V3-PLAN-20260815-FULL.3"` | sample-identity hash input; the public `protocol_version` is `Benchmark-V3-1.0` |

Renaming any of the hash inputs would renumber all 18,624 samples and break
every published file name, so they are pinned deliberately. Fields that only
exist in the internal ledger never reach `dataset/`: the packager selects a
fixed metadata whitelist and rewrites every published naming field.

A repository-wide search for `full_3` / `full3` therefore returns exactly these
five lines in `plan.py` plus this document. Nothing in `dataset/`, the
evaluation package or the public documentation contains them.

One further internal token appears in `generation/package_dataset.py`:
`INTERNAL_FAMILY_PREFIX = "full_3_"`. It normalises the `display_name` of the
frozen reference run, which was captured before the public naming contract.
Runs produced by this repository already emit the public family name, so the
normalisation is a no-op for them; it exists so the historical run stays
packageable byte-identically.

`generation/benchmark_generator/carla_client/carla_helpers.py` also retains a
`_execution_semantics()` fallback whose string values mention the internal
pilot stage. That fallback is only used when a caller supplies no semantics
object; it never writes into `dataset/`.
