from scripts._vendor.rewrite import rewrite_skill_body


def test_rewrites_uv_run_prefix():
    body = (
        "Run:\n```bash\n"
        'uv run --project "${foundry_project:-$(pwd)}" forge-validate data-flow-map \\\n'
        '  --state-dir "$state_dir" "$target"\n```\n'
    )
    out, n = rewrite_skill_body(body)
    assert n == 1
    assert '"${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" data-flow-map' in out
    assert "uv run --project" not in out


def test_localizes_state_dir_default():
    body = 'state_dir=".foundry/context/dfm"\n'
    out, _ = rewrite_skill_body(body)
    assert ".forge-foundry/context/dfm" in out
    assert ".foundry/context" not in out


def test_idempotent():
    body = '```bash\nuv run --project "$x" forge-validate repo-digest .\n```\n'
    once, n1 = rewrite_skill_body(body)
    twice, n2 = rewrite_skill_body(once)
    assert once == twice
    assert n1 == 1 and n2 == 0


def test_leaves_prose_untouched():
    body = "This skill delegates to forge-validate under the hood.\n"
    out, n = rewrite_skill_body(body)
    assert out == body and n == 0
