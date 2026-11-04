"""Frozen E3 site-macro, cluster-bootstrap and paired significance utilities."""
from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict

BASE_SEED = 20260811


def stable_seed(key: str, base: int = BASE_SEED) -> int:
    return (base + int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big")) % (2**32)


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] if low == high else ordered[low] * (high - position) + ordered[high] * (position - low)


def bootstrap_sites(site_values: dict[str, float], key: str, replicates: int = 10000) -> dict:
    if not site_values:
        raise ValueError("empty sites")
    sites = sorted(site_values)
    rng = random.Random(stable_seed(key))
    draws = [sum(site_values[rng.choice(sites)] for _ in sites) / len(sites) for _ in range(replicates)]
    return {"estimate": sum(site_values.values()) / len(sites), "ci95": [percentile(draws, .025), percentile(draws, .975)],
            "sites": len(sites), "replicates": replicates, "seed": stable_seed(key)}


def road_site_values(rows: list[dict], metric: str) -> dict[str, float]:
    anchors = defaultdict(list)
    for row in rows:
        anchors[(row["site_id"], row["anchor_id"])].append(float(row[metric]))
    sites = defaultdict(list)
    for (site, _), values in anchors.items():
        sites[site].append(sum(values) / len(values))
    return {site: sum(values) / len(values) for site, values in sites.items()}


def lane_site_values(rows: list[dict]) -> dict[str, float]:
    anchors = defaultdict(lambda: [0, 0, 0])
    for row in rows:
        cell = anchors[(row["site_id"], row["anchor_id"])]
        cell[0] += int(row["tp"]); cell[1] += int(row["fp"]); cell[2] += int(row["fn"])
    sites = defaultdict(list)
    for (site, _), (tp, fp, fn) in anchors.items():
        den = 2 * tp + fp + fn
        sites[site].append(2 * tp / den if den else 1.0)
    return {site: sum(values) / len(values) for site, values in sites.items()}


def sign_flip(a: dict[str, float], b: dict[str, float], key: str, draws: int = 10000) -> dict:
    sites = sorted(set(a) & set(b))
    delta = [a[site] - b[site] for site in sites]
    observed = sum(delta) / len(delta)
    rng = random.Random(stable_seed(key))
    extreme = 0
    for _ in range(draws):
        value = sum(d * (1 if rng.getrandbits(1) else -1) for d in delta) / len(delta)
        extreme += abs(value) >= abs(observed)
    return {"mean_difference": observed, "raw_p": (extreme + 1) / (draws + 1), "sites": len(sites), "draws": draws, "seed": stable_seed(key)}


def holm(results: list[dict], alpha: float = .05) -> list[dict]:
    ordered = sorted(enumerate(results), key=lambda item: item[1]["raw_p"])
    adjusted = [0.0] * len(results); running = 0.0; count = len(results)
    for rank, (index, result) in enumerate(ordered):
        running = max(running, min(1.0, (count - rank) * result["raw_p"]))
        adjusted[index] = running
    return [{**result, "holm_adjusted_p": adjusted[index], "reject_fwer_0_05": adjusted[index] <= alpha}
            for index, result in enumerate(results)]
