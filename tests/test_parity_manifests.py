import json


def test_versions_agree(repo_root):
    pj = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text())
    mk = json.loads((repo_root / ".claude-plugin" / "marketplace.json").read_text())
    lock = json.loads((repo_root / "engine" / "VENDOR_LOCK.json").read_text())
    assert pj["version"] == mk["plugins"][0]["version"] == lock["version"]
