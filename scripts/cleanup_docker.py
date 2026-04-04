#!/usr/bin/env python3
"""
Docker Disk Cleanup

Frees Docker disk space by removing:
  - Dangling images (untagged intermediate layers)
  - Build cache
  - Non-latest tags on project agent images (e.g., :test, :old)

Preserves all :latest tagged images unconditionally.

Usage:
    python scripts/cleanup_docker.py [--dry-run]
"""

import argparse
import json
import subprocess
import sys


def run(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


def get_disk_usage() -> dict:
    code, out = run(["docker", "system", "df", "--format", "{{json .}}"])
    if code != 0:
        return {}
    usage = {}
    for line in out.splitlines():
        try:
            item = json.loads(line)
            usage[item.get("Type")] = item
        except json.JSONDecodeError:
            pass
    return usage


def format_usage(usage: dict) -> str:
    lines = []
    for type_name in ("Images", "Build Cache", "Containers", "Local Volumes"):
        item = usage.get(type_name, {})
        if item:
            lines.append(
                f"  {type_name:<16} {item.get('Size', '?'):>10}  "
                f"(reclaimable: {item.get('Reclaimable', '?')})"
            )
    return "\n".join(lines)


def get_non_latest_agent_images() -> list[dict]:
    """Return agent images that are not tagged :latest."""
    code, out = run([
        "docker", "images",
        "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}",
    ])
    if code != 0:
        return []
    results = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        ref, image_id, size = parts
        repo, _, tag = ref.rpartition(":")
        if repo.endswith("-agent") and tag != "latest":
            results.append({"ref": ref, "id": image_id, "size": size})
    return results


def prune_dangling(dry_run: bool) -> str:
    images = get_dangling_count()
    if images == 0:
        return "  No dangling images."
    if dry_run:
        return f"  Would remove {images} dangling image(s)."
    code, out = run(["docker", "image", "prune", "-f"])
    return f"  {out.splitlines()[-1]}" if out else "  Done."


def get_dangling_count() -> int:
    code, out = run(["docker", "images", "-f", "dangling=true", "-q"])
    return len(out.splitlines()) if out else 0


def prune_build_cache(dry_run: bool) -> str:
    code, out = run(["docker", "builder", "du", "--verbose"])
    if dry_run:
        # Just show estimated size
        code2, out2 = run(["docker", "system", "df"])
        for line in out2.splitlines():
            if "Build Cache" in line:
                return f"  Would prune build cache: {line.split()[3]}"
        return "  Would prune build cache."
    print("  Pruning build cache (may take a moment)...", flush=True)
    code, out = run(["docker", "builder", "prune", "-f"])
    last = out.splitlines()[-1] if out else "Done."
    return f"  {last}"


def remove_non_latest_agent_images(dry_run: bool) -> list[str]:
    images = get_non_latest_agent_images()
    if not images:
        return ["  No non-latest agent images found."]
    lines = []
    for img in images:
        if dry_run:
            lines.append(f"  Would remove {img['ref']} ({img['size']})")
        else:
            code, out = run(["docker", "rmi", img["ref"]])
            status = "removed" if code == 0 else f"failed: {out}"
            lines.append(f"  {img['ref']} ({img['size']}) — {status}")
    return lines


def main():
    parser = argparse.ArgumentParser(description="Clean up Docker disk space")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be removed without doing it")
    args = parser.parse_args()

    dry_run = args.dry_run
    prefix = "[DRY RUN] " if dry_run else ""

    print(f"\n{prefix}Docker Disk Cleanup")
    print("=" * 50)

    print("\nBefore:")
    before = get_disk_usage()
    print(format_usage(before))

    print("\n--- Dangling images ---")
    print(prune_dangling(dry_run))

    print("\n--- Non-latest agent image tags ---")
    for line in remove_non_latest_agent_images(dry_run):
        print(line)

    print("\n--- Build cache ---")
    print(prune_build_cache(dry_run))

    if not dry_run:
        print("\nAfter:")
        after = get_disk_usage()
        print(format_usage(after))

    print()


if __name__ == "__main__":
    main()
