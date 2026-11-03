"""Verify retained evidence and recompute the final report after relocation.

Recreate the recorded Diploma layout in a temporary directory. Accept only
recorded source hashes or explicitly documented post-live source changes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[1]
EVIDENCE = REPOSITORY / "docs/verification"
FINAL = "final-fixes-2026-11-03"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_archives():
    checked = 0
    manifests = [*EVIDENCE.glob("*/artifact-sha256.json"),
                 *EVIDENCE.glob("*/artifacts-sha256.json")]
    for manifest in sorted(manifests):
        for name, expected in json.loads(manifest.read_text()).items():
            path = (manifest.parent / name).resolve()
            if not path.is_relative_to(manifest.parent.resolve()):
                raise ValueError(f"Artifact escapes archive: {name}")
            if digest(path) != expected:
                raise ValueError(f"Artifact hash mismatch: {path}")
            checked += 1
    print(f"Verified {checked} archived artifact hashes", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-repo", type=Path, default=REPOSITORY.parent / "telegram-review-bot")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    verify_archives()
    if args.verify_only:
        return
    archive = EVIDENCE / FINAL
    protocol = json.loads((archive / "protocol-v12.json").read_text())
    patch = json.loads((archive / "post-live-format-patch.json").read_text())
    expected = {**protocol["source_sha256"], patch["file"]: patch["after_sha256"]}
    maintenance = json.loads((EVIDENCE / "maintenance-2026-11-03.json").read_text())
    for change in maintenance["source_changes"]:
        if expected.get(change["file"]) != change["before_sha256"]:
            raise ValueError(f"Post-live source change has unexpected baseline: {change['file']}")
        expected[change["file"]] = change["after_sha256"]
    with tempfile.TemporaryDirectory(prefix="review-verification-") as temporary:
        workspace = Path(temporary)
        for name, sha in expected.items():
            relative = Path(name)
            if relative.parts[0] == "Review_bot":
                source = REPOSITORY.joinpath(*relative.parts[1:])
            elif name == "scripts/live_agent_acceptance.py":
                source = args.bot_repo / name
            else:
                raise ValueError(f"Unmapped recorded source: {name}")
            if source.is_file() and digest(source) != sha and name == "Review_bot/app/services/review_eval.py":
                # The relocation commit removes one redundant EOF newline.
                # Reconstruct the exact recorded bytes only for that known edit;
                # any content change still fails verification.
                snapshot = EVIDENCE / "source-snapshots/review_eval.py"
                if (digest(snapshot) == sha
                        and source.read_bytes() == snapshot.read_bytes().rstrip(b"\r\n") + b"\n"):
                    source = snapshot
            if not source.is_file() or digest(source) != sha:
                raise ValueError(f"Recorded source missing or changed: {source}")
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        target = workspace / "docs/verification" / FINAL
        shutil.copytree(archive, target)
        # Only the temporary protocol uses documented post-live source hashes.
        # The stored live protocol and measurements remain unchanged.
        for change in maintenance["source_changes"]:
            protocol["source_sha256"][change["file"]] = change["after_sha256"]
        (target / "protocol-v12.json").write_text(json.dumps(protocol))
        subprocess.run([sys.executable, str(target / "summarize.py")], check=True, cwd=workspace)
        for name in ("summary.json", "regression-v12-cases.csv", "fresh-cases.csv"):
            if digest(target / name) != digest(archive / name):
                raise ValueError(f"Recomputed output differs: {name}")
    print("Recomputed final metrics match the archive; source hashes and documented post-live changes verified")


if __name__ == "__main__":
    main()
