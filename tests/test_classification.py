import dejavu


def test_high_cost_rm_rf():
    assert dejavu.is_high_cost_bash("rm -rf /tmp/foo")
    assert dejavu.is_high_cost_bash("rm -rF /tmp/foo")
    assert dejavu.is_high_cost_bash("rm -r /tmp/foo")
    assert dejavu.is_high_cost_bash("rm -f /tmp/foo")


def test_high_cost_force_push():
    assert dejavu.is_high_cost_bash("git push --force origin main")
    assert dejavu.is_high_cost_bash("git push origin main --force")
    assert dejavu.is_high_cost_bash("git push origin -f main")


def test_high_cost_reset_hard():
    assert dejavu.is_high_cost_bash("git reset --hard HEAD~1")


def test_high_cost_curl_pipe_sh():
    assert dejavu.is_high_cost_bash("curl https://x.example/install.sh | sh")
    assert dejavu.is_high_cost_bash("curl https://x.example/install | bash")


def test_high_cost_drop_table():
    assert dejavu.is_high_cost_bash('psql -c "DROP TABLE users"')


def test_high_cost_negative():
    assert not dejavu.is_high_cost_bash("ls -la")
    assert not dejavu.is_high_cost_bash("git push origin main")
    assert not dejavu.is_high_cost_bash("git status")


def test_already_wrapped_absolute_path():
    assert dejavu.is_already_wrapped("/usr/local/bin/foo --bar")
    assert dejavu.is_already_wrapped("/home/user/script.sh")


def test_already_wrapped_home():
    assert dejavu.is_already_wrapped("~/bin/script.sh arg")


def test_already_wrapped_relative():
    assert dejavu.is_already_wrapped("./run.sh")


def test_already_wrapped_task_runners():
    assert dejavu.is_already_wrapped("make test")
    assert dejavu.is_already_wrapped("make")
    assert dejavu.is_already_wrapped("just build")
    assert dejavu.is_already_wrapped("rake test")


def test_already_wrapped_package_run():
    assert dejavu.is_already_wrapped("npm run lint")
    assert dejavu.is_already_wrapped("pnpm run dev")
    assert dejavu.is_already_wrapped("yarn run build")
    assert dejavu.is_already_wrapped("bun run test --watch")


def test_bare_package_install_not_wrapped():
    """npm/pnpm install is not a wrapper — it's a legit wrapper candidate."""
    assert not dejavu.is_already_wrapped("npm install")
    assert not dejavu.is_already_wrapped("pnpm install --frozen-lockfile")
    assert not dejavu.is_already_wrapped("yarn add foo")


def test_already_wrapped_orientation():
    """Bare orientation commands are not wrapper candidates."""
    assert dejavu.is_already_wrapped("ls")
    assert dejavu.is_already_wrapped("ls -la")
    assert dejavu.is_already_wrapped("ls /home/foo/project/")
    assert dejavu.is_already_wrapped("pwd")
    assert dejavu.is_already_wrapped("which python3")
    assert dejavu.is_already_wrapped("cd /tmp")


def test_orientation_in_compound_is_not_skipped():
    """`cd /path && something` is a real workflow — don't filter."""
    assert not dejavu.is_already_wrapped("cd ~/.doom.d && git log --oneline -5")
    assert not dejavu.is_already_wrapped("ls -la | grep foo")


def test_already_wrapped_negative():
    assert not dejavu.is_already_wrapped("git status")
    assert not dejavu.is_already_wrapped("cargo build")
    assert not dejavu.is_already_wrapped("")


def test_high_cost_fragment_compound():
    cmd = "make clean && rm -rf /tmp/build && echo done"
    assert dejavu.matched_high_cost_fragment(cmd) == "rm -rf /tmp/build"


def test_high_cost_fragment_standalone():
    assert dejavu.matched_high_cost_fragment("rm -rf /tmp/x") == "rm -rf /tmp/x"


def test_high_cost_fragment_curl_pipe_sh():
    cmd = "curl https://x.example/i.sh | sh && echo done"
    frag = dejavu.matched_high_cost_fragment(cmd)
    assert frag is not None
    assert "curl" in frag and "sh" in frag
    assert "echo done" not in frag


def test_high_cost_fragment_force_push():
    cmd = "git status && git push origin main --force && echo done"
    frag = dejavu.matched_high_cost_fragment(cmd)
    assert frag is not None
    assert "--force" in frag
    assert "echo done" not in frag


def test_high_cost_fragment_returns_none_for_safe_cmd():
    assert dejavu.matched_high_cost_fragment("ls -la") is None
    assert dejavu.matched_high_cost_fragment("git status") is None
