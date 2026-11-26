"""One-time pinned download & SHA-256 verification of amazon/chronos-2 for offline Docker mounting."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

MODEL_ID = "amazon/chronos-2"
REVISION = "29ec3766d36d6f73f0696f85560a422f50e8498c"
LICENSE = "apache-2.0"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_cli_path(raw: str) -> Path:
    if "\x00" in raw or ".." in Path(raw).parts:
        raise ValueError(f"Unsafe path argument: {raw}")
    resolved = Path(raw).resolve()
    allowed_roots = (REPO_ROOT, Path.cwd().resolve(), Path(tempfile.gettempdir()).resolve())
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
        raise ValueError(f"Path outside allowed workspace/temp directories: {resolved}")
    return resolved


def verify_files(target: Path, manifest: dict) -> dict:
    cmeta = manifest.get("chronos2") or {}
    expected_hashes = cmeta.get("files_sha256") or {}
    expected_bytes = cmeta.get("files_bytes") or {}
    if not expected_hashes:
        raise ValueError("Committed manifest missing chronos2.files_sha256 checksums.")

    base = _resolve_cli_path(str(target))
    verified = {}
    for rel, exp_sha in expected_hashes.items():
        if "\x00" in rel or ".." in Path(rel).parts:
            raise ValueError(f"Unsafe manifest entry path: {rel}")
        fpath = (base / rel).resolve()
        if not fpath.is_relative_to(base):
            raise ValueError(f"Manifest entry escapes target directory: {rel}")
        if not fpath.exists():
            raise FileNotFoundError(f"Expected model file missing: {fpath}")
        data = fpath.read_bytes()
        actual_sha = hashlib.sha256(data).hexdigest()
        actual_len = len(data)
        if actual_sha != exp_sha:
            fpath.unlink(missing_ok=True)
            raise ValueError(
                f"SHA-256 mismatch for {rel}: expected {exp_sha}, got {actual_sha}. Removed unverified file."
            )
        if rel in expected_bytes and actual_len != int(expected_bytes[rel]):
            fpath.unlink(missing_ok=True)
            raise ValueError(
                f"Byte size mismatch for {rel}: expected {expected_bytes[rel]}, got {actual_len}. Removed unverified file."
            )
        verified[rel] = {"sha256": actual_sha, "bytes": actual_len}
    return verified


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="ml-service/weights/chronos-2")
    parser.add_argument("--manifest", default="ml-service/model-manifest.json")
    parser.add_argument("--verify-only", action="store_true", help="Verify local weights against manifest without downloading.")
    parser.add_argument("--update-manifest", action="store_true", help="Write newly computed hashes to manifest (maintainer use only).")
    args = parser.parse_args()

    target = _resolve_cli_path(args.target)
    manifest_path = _resolve_cli_path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    if not args.verify_only:
        from huggingface_hub import snapshot_download

        target.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=MODEL_ID,
            revision=REVISION,
            local_dir=str(target),
            allow_patterns=["config.json", "model.safetensors", "README.md", ".gitattributes"],
        )

    if args.update_manifest:
        checksums = {}
        sizes = {}
        for rel in ("config.json", "model.safetensors"):
            fpath = (target / rel).resolve()
            if not fpath.is_relative_to(target):
                raise ValueError(f"Unsafe artifact filename: {rel}")
            if not fpath.exists():
                raise FileNotFoundError(f"Expected file missing after download: {fpath}")
            data = fpath.read_bytes()
            checksums[rel] = hashlib.sha256(data).hexdigest()
            sizes[rel] = len(data)
        manifest["chronos2"] = {
            "model_id": MODEL_ID,
            "revision": REVISION,
            "license": LICENSE,
            "source_url": f"https://huggingface.co/{MODEL_ID}",
            "weights_format": "safetensors",
            "files_sha256": checksums,
            "files_bytes": sizes,
            "limitations": (
                "120M zero-shot foundation model; pretrained on autogluon/chronos_datasets & Salesforce/GiftEvalPretrain. "
                "Inference latency on CPU scales with context length and series count; evaluated honestly against seasonal baseline."
            ),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"updated_manifest": True, "chronos2": manifest["chronos2"]}, indent=2))
        return

    verified = verify_files(target, manifest)
    print(json.dumps({"verified": True, "model_id": MODEL_ID, "revision": REVISION, "files": verified}, indent=2))


if __name__ == "__main__":
    main()
