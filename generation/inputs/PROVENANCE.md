# Frozen planning inputs

These files are the complete offline input set for planning. `input_checksums.json`
pins each file; the planner fails closed if any checksum drifts.

| File | Purpose |
|---|---|
| `site_registry.json` | 309 finalization sites: map, structural cell, leakage group, anchor ids |
| `anchor_registry.json` | 423 finalization anchors: site, map, lane count, subset assignment |
| `road_library.json` | precomputed road geometry used for corridor and projection checks |
| `split_decision.json` | frozen train / val / test site assignment |
| `site_structural_cells.json` | per-site structural cell actually used by the plan |
| `test_anchor_classification.json` | per-test-anchor yaw sign, original-core flag, reference conditions |
| `maps/Town04_Opt.xodr` | OpenDRIVE for map 1 |
| `maps/Town05_Opt.xodr` | OpenDRIVE for map 2 |
| `maps/Town10HD_Opt.xodr` | OpenDRIVE for map 3 |

`input_checksums.json` lists the SHA-256 of all nine files.

## Two derived files

### `site_structural_cells.json`

The finalization site registry predates a scene reclassification. For 91 of the
204 planned sites the registry's `structural_cell` differs from the value the
frozen plan used, and `structural_cell` determines the reported `scene` and the
per-scene expected counts. The plan therefore reads the frozen mapping rather
than the registry field.

Shape:

```json
{
  "schema_version": "benchmark_v3_site_structural_cells_1.0",
  "source": "frozen planning ledger after scene reclassification",
  "sites": {"site_...": "Straight-8Lane", "...": "..."}
}
```

### `test_anchor_classification.json`

The planner needs three facts per test anchor that are not derivable from the
site registry:

- `yaw_sign` — which way the corridor is offset relative to the road direction;
- `is_orig_core` — whether the anchor belongs to the original Core bundle;
- the frozen reference `yaw_relative_deg`, including its **float sign**.

The last point matters: 93 anchors have `0.0` and 33 have `-0.0`. The recipe id
hashes the JSON representation of the conditions, so `-0.0` and `0.0` produce
different ids. The file stores the exact value per anchor.

All other reference condition fields are identical across the 126 anchors and
are stored once under `reference_conditions`.

Shape:

```json
{
  "schema_version": "benchmark_v3_test_anchor_classification_1.0",
  "reference_conditions": {"height_m": 20.0, "pitch_deg": -80.0, "...": "..."},
  "anchors": [
    {"anchor_id": "anchor_...", "yaw_sign": -1, "is_orig_core": true, "yaw_relative_deg": -0.0}
  ]
}
```

## OpenDRIVE paths

`road_library.json` stores map paths as `inputs/maps/<map>.xodr`, relative to
this package root. The maps are byte-identical to the ones used by the frozen
run; their checksums are also recorded in `dataset/dataset_card.json`.
