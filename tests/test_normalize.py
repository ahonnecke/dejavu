import dejavu


def test_strips_tail_pipe():
    assert dejavu.normalize_cmd("make test 2>&1 | tail -10") == "make test"
    assert dejavu.normalize_cmd("make test | tail -25") == "make test"


def test_strips_head_pipe():
    assert dejavu.normalize_cmd("git log | head -5") == "git log"


def test_strips_dev_null_redirects():
    assert dejavu.normalize_cmd("foo > /dev/null") == "foo"
    assert dejavu.normalize_cmd("foo 2> /dev/null") == "foo"


def test_strips_2to1():
    assert dejavu.normalize_cmd("foo 2>&1") == "foo"


def test_collapses_uuid():
    cmd = "rm /tmp/abc-12345678-1234-1234-1234-123456789abc-end"
    assert "<UUID>" in dejavu.normalize_cmd(cmd)


def test_idempotent_on_simple_commands():
    assert dejavu.normalize_cmd("git status") == "git status"
    assert dejavu.normalize_cmd("ls -la") == "ls -la"


def test_repeats_strip_layers():
    # Multiple suffixes should all be removed
    assert dejavu.normalize_cmd("foo 2>&1 | tail -10 > /dev/null") == "foo"
