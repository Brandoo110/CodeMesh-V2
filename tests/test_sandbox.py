"""
沙箱单元测试（不依赖 pytest）
================================

跑法：
    python -m tests.test_sandbox
"""

from execution.sandbox import check_command, SandboxViolation


# ────────────────────────── helpers ──────────────────────────


def _expect_blocked(cmd: str) -> None:
    """断言 cmd 会被沙箱拦截。"""
    try:
        check_command(cmd)
    except SandboxViolation:
        return  # OK
    raise AssertionError(f"expected SandboxViolation for: {cmd!r}")


def _expect_allowed(cmd: str) -> None:
    """断言 cmd 通过沙箱（不抛）。"""
    try:
        check_command(cmd)
    except SandboxViolation as e:
        raise AssertionError(f"unexpectedly blocked: {cmd!r} ({e.reason})")


# ────────────────────────── 应该被拦的危险命令 ──────────────────────────


def test_blocks_rm_rf_root():
    _expect_blocked("rm -rf /")


def test_blocks_rm_rf_home():
    _expect_blocked("rm -rf ~")


def test_blocks_rm_rf_glob():
    _expect_blocked("rm -rf *")


def test_blocks_sudo():
    _expect_blocked("sudo apt install whatever")


def test_blocks_mkfs():
    _expect_blocked("mkfs.ext4 /dev/sda1")


def test_blocks_dd_to_disk():
    _expect_blocked("dd if=/dev/zero of=/dev/sda")


def test_blocks_fork_bomb():
    _expect_blocked(":(){ :|:& };:")


def test_blocks_drop_table_uppercase():
    _expect_blocked("psql -c 'DROP TABLE users;'")


def test_blocks_drop_table_lowercase():
    _expect_blocked("psql -c 'drop table users;'")


def test_blocks_truncate_table():
    _expect_blocked("mysql -e 'TRUNCATE TABLE orders;'")


def test_blocks_chmod_777_root():
    _expect_blocked("chmod -R 777 /")


def test_blocks_write_to_disk_device():
    _expect_blocked("echo hi > /dev/sda")


# ────────────────────────── 应该放行的安全命令 ──────────────────────────


def test_allows_normal_ls():
    _expect_allowed("ls -la")


def test_allows_normal_pytest():
    _expect_allowed("pytest tests/ -v")


def test_allows_targeted_rm_in_tmp():
    """rm 不带 -rf 且不指向根 / 家目录 / 通配 → 安全。"""
    _expect_allowed("rm /tmp/specific-file.txt")


def test_allows_git_commands():
    _expect_allowed("git status")
    _expect_allowed("git log -10 --oneline")


def test_allows_safe_chmod():
    """非 `-R 777 /` 的 chmod 应放行。"""
    _expect_allowed("chmod 644 myfile.txt")


def test_allows_safe_select():
    """SQL SELECT 不在黑名单。"""
    _expect_allowed("psql -c 'SELECT * FROM users LIMIT 5;'")


# ────────────────────────── exception 字段 ──────────────────────────


def test_violation_carries_command_and_reason():
    try:
        check_command("rm -rf /")
    except SandboxViolation as e:
        assert e.command == "rm -rf /"
        assert "dangerous pattern" in e.reason
        return
    raise AssertionError("SandboxViolation not raised")


# ────────────────────────── runner ──────────────────────────


if __name__ == "__main__":
    import traceback

    tests = [
        v for k, v in list(globals().items())
        if callable(v) and k.startswith("test_")
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK  {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} sandbox tests passed.")
    if failed:
        raise SystemExit(1)
