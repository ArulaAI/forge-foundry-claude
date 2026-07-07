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


def test_state_dir_default_is_preserved():
    # The state dir must NOT be rewritten: the engine's Python defaults and every
    # cross-primitive read use `.foundry/context` (CWD-relative). Rewriting only the
    # docs to `.forge-foundry/context` broke the M-tier chain, so it was reverted.
    body = 'state_dir=".foundry/context/dfm"\n'
    out, _ = rewrite_skill_body(body)
    assert out == body
    assert ".foundry/context/dfm" in out
    assert ".forge-foundry/context" not in out


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


def test_rewrites_bare_forge_validate_command_value():
    body = "command: forge-validate mod-db-schema-analyze\n"
    out, n = rewrite_skill_body(body)
    assert n == 1
    assert 'command: "${CLAUDE_PLUGIN_ROOT}/bin/forge-validate" mod-db-schema-analyze' in out
    once, n2 = rewrite_skill_body(out)
    assert once == out and n2 == 0


def test_rewrites_python_m_primitives():
    body = "command: python -m primitives.legacy_modernization.shared.report_aggregator\n"
    out, n = rewrite_skill_body(body)
    assert n == 1
    assert (
        'command: "${CLAUDE_PLUGIN_ROOT}/engine/.venv/bin/python" '
        "-m primitives.legacy_modernization.shared.report_aggregator" in out
    )
    once, n2 = rewrite_skill_body(out)
    assert once == out and n2 == 0


def test_leaves_python_m_pytest_untouched():
    body = "Run `python -m pytest tests/ -q` to check.\n"
    out, n = rewrite_skill_body(body)
    assert out == body and n == 0
