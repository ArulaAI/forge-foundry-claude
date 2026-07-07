#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._vendor.copy_engine import copy_engine
from scripts._vendor.copy_skills import copy_agents, copy_skills, discover_primitives
from scripts._vendor.lock import resolve_source, write_lock
from scripts._vendor.rewrite import rewrite_reference_files, rewrite_skills_tree


def _patch_version(repo: Path, version: str) -> None:
    for rel in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
        path = repo / rel
        data = json.loads(path.read_text())
        if "version" in data:
            data["version"] = version
        if "plugins" in data:
            for p in data["plugins"]:
                p["version"] = version
        path.write_text(json.dumps(data, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Vendor forge-foundry-primitives into this plugin.")
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--allow-dirty", action="store_true")
    ap.add_argument("--version", default="0.1.0")
    args = ap.parse_args(argv)

    repo = Path(__file__).resolve().parent.parent
    info = resolve_source(args.source, args.ref, allow_dirty=args.allow_dirty)
    prims = discover_primitives(args.source / "primitives")

    n_skills = copy_skills(prims, repo / "skills")
    n_agents = copy_agents(prims, repo / "agents", args.source)
    copy_engine(args.source, prims, repo / "engine", version=args.version)
    counts = rewrite_skills_tree(repo / "skills")
    ref_counts = rewrite_reference_files(repo / "skills")

    write_lock(repo / "engine", info, primitive_count=n_skills, version=args.version)
    _patch_version(repo, args.version)

    rewritten = sum(counts.values())
    ref_rewritten = sum(ref_counts.values())
    print(
        f"vendored {n_skills} skills, {n_agents} agents; "
        f"{rewritten} PLB invocations rewritten in SKILL.md, "
        f"{ref_rewritten} in reference files"
    )
    print(f"source {info.ref}@{info.sha[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
