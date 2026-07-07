

def test_repo_root_has_scripts(repo_root):
    assert (repo_root / "scripts").is_dir()


def test_gitignore_excludes_venv(repo_root):
    gi = (repo_root / ".gitignore").read_text()
    assert "engine/.venv" in gi
