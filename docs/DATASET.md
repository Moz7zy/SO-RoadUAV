# Dataset protocol

## 1. Tasks and label semantics

Benchmark-V3 publishes exactly two supervised targets. There are no paired
Visible/Full variants and no occlusion-completion targets.

### Road-region segmentation

`road_mask_visible` is a 1280×720 PNG with values `0` and `255`. Foreground is
the CARLA semantic mask of the rendered frame for `Roads=1` and `RoadLines=24`:

```python
foreground = (semantic == 1) | (semantic == 24)
```

The mask describes what is visible in that frame. It is not an
occlusion-completed or map-projected road polygon.

### Target-corridor lane recovery

`lane_polyline_projected` is a JSON document:

```json
{
  "sample_id": "v3_...",
  "protocol_version": "Benchmark-V3-1.0",
  "label_rule_version": "lane_marking_projection_default_1.0",
  "visibility_semantics": "projected_in_image_no_occlusion_test",
  "lane_instance_count": 2,
  "instances": [
    {"instance_id": "lane_marking_0000", "points_image": [[x, y], ...], "...": "..."}
  ]
}
```

Each instance is **one distinct physical lane marking**, projected into the
image and clipped to the target corridor. Points are in original image
coordinates (1280×720), not normalised and not in a resized working space.

The evaluation ROI is
`metadata.labels.lane_polyline_projected.target_corridor_polygon`: a list of
convex polygons in image coordinates. A prediction outside this ROI is not
target-corridor evidence.

Downstream consumers must **not** merge or split instances: the published
instances are already the final physical-marking set.

## 2. Directory layout

```
dataset/
├── train|val|test/
│   ├── rgb/<sample_id>.jpg                 1280×720 JPEG
│   ├── road_mask_visible/<sample_id>.png   0/255 PNG
│   ├── lane_polyline_projected/<sample_id>.json
│   └── metadata/<sample_id>.json
├── manifests/
│   ├── subsets/{train,val,test}.jsonl
│   └── views/{core_scene,core_observation,core_axis,challenge_l18}.jsonl
├── dataset_card.json
├── package_manifest.json
├── README.md / README_CN.md
└── SHA256SUMS
```

Each sample appears exactly once. The view manifests are logical indexes over
the same `test/` assets and contain no duplicated images.

**Distribution.** The package is hosted on Hugging Face
(`Mozzzy/SO-RoadUAV`). In the Git repository, `dataset/` is a download target
and is not tracked; see [`DOWNLOAD.md`](DOWNLOAD.md).

**Never build the dataset by scanning directories.** The manifests carry sample
identity, subset routing and slice membership.

### Subset manifest row

```json
{
  "sample_id": "v3_...",
  "rgb": "test/rgb/v3_....jpg",
  "road_mask": "test/road_mask_visible/v3_....png",
  "lane": "test/lane_polyline_projected/v3_....json",
  "metadata": "test/metadata/v3_....json",
  "scene": "straight_5_8",
  "subset": "test",
  "conditions": {"height_m": 20.0, "pitch_deg": -80.0, "yaw_relative_deg": 0.0,
                 "yaw_magnitude_deg": 0.0, "traffic_density": 0.0,
                 "weather": "dry", "illumination_sun_altitude_deg": 75.0},
  "control_bundle_id": "bundle_...",
  "reference_sample_id": "v3_...",
  "is_reference": true,
  "changed_factor": "reference",
  "factor_level": "default",
  "factor_levels": {},
  "qa_status": "accepted",
  "label_rule_version": "lane_marking_projection_default_1.0"
}
```

### Metadata

`metadata/<sample_id>.json` holds the identity and provenance fields the
protocol depends on:

| Group | Fields |
|---|---|
| identity | `sample_id`, `display_name`, `subset`, `site_id`, `anchor_id`, `map_id`, `leakage_group_id` |
| scene | `scene`, `road_layout`, `lane_configuration`, `structural_cell` |
| observation | `requested_conditions`, `actual_conditions`, `observation_recipe_id`, `control_bundle_id` |
| slice | `is_reference`, `reference_sample_id`, `changed_factor`, `factor_level`, `factor_levels`, `challenge_row_id`, `challenge_level_tuple`, `challenge_axis_id` |
| labels | `labels.road_mask_visible`, `labels.lane_polyline_projected` (path, semantics, `target_corridor_polygon`) |
| QA | `qa`, `strict_geometry`, `traffic_summary` |
| versioning | `protocol_version`, `label_rule_version`, `schema_version`, `paths` |

## 3. Composition

| Subset | Images | Sites | Anchors | Leakage groups |
|---|---:|---:|---:|---:|
| train | 12,000 | 131 | 250 | 55 |
| val | 1,584 | 20 | 33 | — |
| test | 5,040 | 53 | 126 | 25 |

The test split uses a shared pool: 53 sites, 126 anchors, 25 leakage groups,
40 unique images per anchor (41 logical slots, because the reference image is
also aliased into the challenge slice).

Scenes: `straight_1_4`, `straight_5_8`, `curve_1_4`, `curve_5_8`,
`intersection`, `ramp`.

Scene distribution over all 18,624 images:

| Scene | Images |
|---|---:|
| straight_1_4 | 5,696 |
| straight_5_8 | 2,040 |
| curve_1_4 | 6,560 |
| curve_5_8 | 1,912 |
| intersection | 1,456 |
| ramp | 960 |

## 4. Logical views

| View | Images | Membership |
|---|---:|---|
| `core_scene` | 5,040 | all test images, for scene-level aggregation |
| `core_observation` | 1,890 | one-factor-at-a-time slice; 126 anchors × 15 factors |
| `core_axis` | 882 | seven stress axes × 126 anchors |
| `challenge_l18` | 2,268 | 18 multifactor rows × 126 anchors |

`core_observation` rows carry `factor`, `level` and `reference_sample_id`.
`core_axis` rows carry `axis`. `challenge_l18` rows carry `row_id` and the six
factor levels.

## 5. Label rules

Two frozen rule versions appear in the data:

| `label_rule_version` | Samples | Description |
|---|---:|---|
| `lane_marking_projection_default_1.0` | 15,800 | OpenDRIVE lane-marking projection with per-span de-duplication |
| `lane_marking_physical_merge_1.0` | 2,824 | cross-span physical-marking merge at 24 confirmed sites |

The merge rule is applied at generation time and recorded per sample. A special
site drops one erroneous source instance
(`special_drop_source_ref = [11, 0, -2, "right", "Solid"]`). Six additional
sites keep the default path because the frozen OpenDRIVE geometry has no
one-to-one cross-span match.

`dataset_card.json` records the site lists and routing counts.

## 6. Integrity

`SHA256SUMS` covers every payload file, both READMEs, both JSON cards, the
dataset `LICENSE` and the seven manifests (74,508 lines). Verify with:

```bash
cd dataset && sha256sum -c SHA256SUMS
```

Package-level checksums (`dataset_card.package_manifest_sha256`,
`package_manifest.subset_manifest_checksums`,
`package_manifest.view_manifest_checksums`) are derived, never hand-written.

## 7. Known limitations

- The road mask is a rendered-frame semantic mask. Objects and vehicles occlude
  the road; that occlusion is part of the target.
- Lane instances are projected and clipped to the target corridor. A marking
  outside the corridor is not part of the target, even if it is visible.
- Instance count per image is capped at 10 by the protocol.
- `ramp` scenes are included in `core_scene` but excluded from the
  `challenge_l18` primary aggregate (reported separately as `ramp_descriptive`).
