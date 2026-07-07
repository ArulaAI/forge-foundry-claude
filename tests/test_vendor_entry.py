import json
import subprocess
import sys
from pathlib import Path


def test_plugin_manifest_valid(repo_root):
    pj = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())
    assert pj["name"] == "forge-foundry-claude"
    assert "SessionStart" in pj["hooks"]


def test_marketplace_points_at_self(repo_root):
    mk = json.loads((repo_root / ".claude-plugin" / "marketplace.json").read_text())
    assert mk["plugins"][0]["name"] == "forge-foundry-claude"
    assert mk["plugins"][0]["source"] == "./"


def test_vendor_help_runs(repo_root):
    r = subprocess.run([sys.executable, "scripts/vendor.py", "--help"],
                       cwd=repo_root, capture_output=True, text=True)
    assert r.returncode == 0 and "--source" in r.stdout
