#!/usr/bin/env python3
"""Download and extract the SO-RoadUAV dataset package from Hugging Face.

The Hub limits a Git-backed directory to 10,000 entries, so the package is
published as tar archives under ``archives/``. This script downloads the
archives, verifies them against ``archives/SHA256SUMS``, extracts them into the
destination, and verifies the extracted files against the package-level
``SHA256SUMS``.

Examples
--------
Full package (5.5 GB) into ./dataset::

    python scripts/download_dataset.py

Test split only, enough for scoring (about 0.3 GB)::

    python scripts/download_dataset.py --parts test-eval
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
from pathlib import Path

DEFAULT_REPO_ID = "Mozzzy/SO-RoadUAV"
DEFAULT_DEST = "dataset"

# part -> (archive name, approximate size in GB)
ARCHIVES = {
    "train": ("SO-RoadUAV-1.0-train.tar", 3.6),
    "val": ("SO-RoadUAV-1.0-val.tar", 0.5),
    "test": ("SO-RoadUAV-1.0-test.tar", 1.4),
    "test-eval": ("SO-RoadUAV-1.0-test-eval.tar", 0.24),
}

PART_ARCHIVES = {
    "all": ["test", "val", "train"],
    "test-eval": ["test-eval"],
    "test": ["test"],
    "val": ["val"],
    "train": ["train"],
}

# Loose text artifacts: they are not inside the archives.
TEXT_PATTERNS = [
    "SHA256SUMS",
    "LICENSE",
    "README.md",
    "README_CN.md",
    "dataset_card.json",
    "package_manifest.json",
    "manifests/**",
    "archives/SHA256SUMS",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and extract the SO-RoadUAV dataset package.",
    )
    parser.add_argument(
        "--repo-id",
        default=DEFAULT_REPO_ID,
        help=f"Hugging Face dataset repository (default: {DEFAULT_REPO_ID})",
    )
    parser.add_argument(
        "--dest",
        default=DEFAULT_DEST,
        help=f"local dataset directory (default: {DEFAULT_DEST})",
    )
    parser.add_argument(
        "--parts",
        choices=sorted(PART_ARCHIVES),
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
        action=argparse.BooleanOptionalAction,
        default=True,
        help="verify the extracted files against SHA256SUMS (default: on)",
    )
    parser.add_argument(
        "--keep-archives",
        action="store_true",
        help="keep the downloaded tar archives after a successful run",
    )
    parser.add_argument(
        "--clean-metadata",
        action="store_true",
        help="remove local Hugging Face download metadata after a successful run",
    )
    return parser.parse_args()


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def read_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split("  ", 1)
        checksums[relative] = expected
    return checksums


def download_text(dest: Path, repo_id: str, revision: str | None) -> None:
    from huggingface_hub import snapshot_download

    print("downloading manifests and text artifacts...", flush=True)
    snapshot_download(
        repo_id=repo_id,
        repo_type="dataset",
        revision=revision,
        local_dir=str(dest),
        allow_patterns=TEXT_PATTERNS,
    )


def download_archive(
    part: str, dest: Path, repo_id: str, revision: str | None, expected: dict[str, str]
) -> Path:
    from huggingface_hub import hf_hub_download

    name, size_gb = ARCHIVES[part]
    relative = f"archives/{name}"
    if relative not in expected:
        raise SystemExit(f"error: {relative} is missing from archives/SHA256SUMS")

    download_dir = dest / ".downloads"
    print(f"downloading {name} (~{size_gb:g} GB)...", flush=True)
    path = Path(
        hf_hub_download(
            repo_id=repo_id,
            filename=relative,
            repo_type="dataset",
            revision=revision,
            local_dir=str(download_dir),
        )
    )

    print(f"  verifying archive sha256...", flush=True)
    actual = sha256_file(path)
    if actual != expected[relative]:
        raise SystemExit(
            f"error: checksum mismatch for {relative}\n"
            f"  expected {expected[relative]}\n  actual   {actual}"
        )
    return path


def extract(tar_path: Path, dest: Path) -> None:
    print(f"  extracting {tar_path.name}...", flush=True)
    with tarfile.open(tar_path, "r:") as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # Python < 3.12
            root = dest.resolve()
            for member in tar.getmembers():
                target = (dest / member.name).resolve()
                if not str(target).startswith(str(root)):
                    raise SystemExit(f"error: unsafe path in archive: {member.name}")
            tar.extractall(dest)


def verify(dest: Path, strict: bool) -> int:
    sums_path = dest / "SHA256SUMS"
    if not sums_path.is_file():
        print(f"error: {sums_path} not found; cannot verify", file=sys.stderr)
        return 1

    checksums = read_checksums(sums_path)
    checked = skipped = mismatched = 0
    for relative, expected in checksums.items():
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
    dest.mkdir(parents=True, exist_ok=True)

    try:
        import huggingface_hub  # noqa: F401
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

    download_text(dest, args.repo_id, args.revision)
    expected = read_checksums(dest / "archives" / "SHA256SUMS")

    archives: list[Path] = []
    for part in PART_ARCHIVES[args.parts]:
        archive = download_archive(part, dest, args.repo_id, args.revision, expected)
        archives.append(archive)
        extract(archive, dest)

    status = verify(dest, strict=args.parts == "all") if args.verify else 0

    if status == 0 and not args.keep_archives and args.verify:
        downloads = dest / ".downloads"
        if downloads.exists():
            shutil.rmtree(downloads)
            print("removed downloaded archives")

    if status == 0 and args.clean_metadata:
        metadata = dest / ".cache" / "huggingface"
        if metadata.exists():
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
