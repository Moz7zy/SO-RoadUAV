#!/usr/bin/env python3
"""Download the SO-RoadUAV dataset package from Hugging Face.

The package is published at https://huggingface.co/datasets/Mozzzy/SO-RoadUAV
and mirrors the directory layout expected by evaluation/score_predictions.py:
manifests/, train/, val/, test/, SHA256SUMS, LICENSE, ...

Examples
--------
Full package (5.5 GB) into ./dataset::

    python scripts/download_dataset.py

Test split only, enough for scoring (about 230 MB)::

    python scripts/download_dataset.py --parts test-eval --verify
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

DEFAULT_REPO_ID = "Mozzzy/SO-RoadUAV"
DEFAULT_DEST = "dataset"

# Text artifacts that make the package self-describing and verifiable.
TEXT_FILES = [
    "SHA256SUMS",
    "LICENSE",
    "README.md",
    "README_CN.md",
    "dataset_card.json",
    "package_manifest.json",
]

# Asset patterns per download part. "all" means no filtering.
PART_PATTERNS = {
    "all": None,
    "test-eval": [
        "test/lane_polyline_projected/**",
        "test/metadata/**",
        "test/road_mask_visible/**",
    ],
    "test": ["test/**"],
    "val": ["val/**"],
    "train": ["train/**"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download the SO-RoadUAV dataset package from Hugging Face.",
    )
    parser.add_argument(
        "--repo-id",
        default=DEFAULT_REPO_ID,
        help=f"Hugging Face dataset repository (default: {DEFAULT_REPO_ID})",
    )
    parser.add_argument(
        "--dest",
        default=DEFAULT_DEST,
        help=f"local download directory (default: {DEFAULT_DEST})",
    )
    parser.add_argument(
        "--parts",
        choices=sorted(PART_PATTERNS),
        default="all",
        help="subset to download; 'test-eval' is the scoring-only subset",
    )
    parser.add_argument(
        "--revision",
        default=None,
        help="optional git revision of the dataset repository",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify SHA-256 checksums after downloading (streaming, no extra disk)",
    )
    parser.add_argument(
        "--clean-metadata",
        action="store_true",
        help="remove the local Hugging Face download metadata after a successful run",
    )
    return parser.parse_args()


def build_patterns(parts: str) -> list[str] | None:
    if part_patterns := PART_PATTERNS[parts]:
        # Manifests and the text artifacts are required to resolve samples.
        return part_patterns + ["manifests/**"] + TEXT_FILES
    return None


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def verify(dest: Path, strict: bool) -> int:
    sums_path = dest / "SHA256SUMS"
    if not sums_path.is_file():
        print(f"error: {sums_path} not found; cannot verify", file=sys.stderr)
        return 1

    checked = skipped = mismatched = 0
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split("  ", 1)
        path = dest / relative
        if not path.is_file():
            skipped += 1
            continue
        actual = sha256_file(path)
        if actual != expected:
            mismatched += 1
            print(f"MISMATCH  {relative}", file=sys.stderr)
        checked += 1
        if checked % 5000 == 0:
            print(f"  verified {checked} files...", flush=True)

    print(
        f"verified: {checked} OK, {mismatched} mismatched, {skipped} not downloaded"
    )
    if mismatched:
        print("error: checksum verification failed", file=sys.stderr)
        return 1
    if skipped and strict:
        print(
            "error: the full package was requested but some files are missing",
            file=sys.stderr,
        )
        return 1
    if skipped:
        print("note: partial download; only present files were verified")
    return 0


def main() -> int:
    args = parse_args()
    dest = Path(args.dest).expanduser().resolve()
    patterns = build_patterns(args.parts)

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(
            "error: huggingface_hub is required; install it with "
            "`pip install -r requirements.txt`",
            file=sys.stderr,
        )
        return 1

    print(f"repository: {args.repo_id}")
    print(f"destination: {dest}")
    print(f"parts: {args.parts}")

    dest.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=args.repo_id,
        repo_type="dataset",
        revision=args.revision,
        local_dir=str(dest),
        allow_patterns=patterns,
    )

    status = 0
    if args.verify:
        status = verify(dest, strict=args.parts == "all")

    if status == 0 and args.clean_metadata:
        metadata = dest / ".cache" / "huggingface"
        if metadata.exists():
            import shutil

            shutil.rmtree(metadata)
            print(f"removed download metadata: {metadata}")

    if status == 0:
        print()
        print("next:")
        print(
            "  python evaluation/score_predictions.py "
            f"--dataset {args.dest} --predictions ./preds --model my_model --out ./scores"
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
