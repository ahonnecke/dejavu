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


def test_already_wrapped_negative():
    assert not dejavu.is_already_wrapped("git status")
    assert not dejavu.is_already_wrapped("make test")
    assert not dejavu.is_already_wrapped("npm install")
    assert not dejavu.is_already_wrapped("")
