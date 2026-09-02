"""auth 模块单元测试：文件驱动密码校验、用户名规则、会话生成。"""

import pytest

from app import auth as auth_mod
from app.auth import USERNAME_RE, _check_password, _new_session

from tests.helpers import PASSWORD


class TestCheckPassword:
    """_check_password：文件驱动账号的校验规则。"""

    def test_correct_password(self, users_dir):
        assert _check_password("alice", PASSWORD) is True

    def test_wrong_password(self, users_dir):
        assert _check_password("alice", "wrong") is False

    def test_unknown_user(self, users_dir):
        assert _check_password("nobody", PASSWORD) is False

    def test_password_stripped_on_both_ends(self, users_dir):
        # 文件首尾空白被忽略（编辑器常见尾换行）；输入端不 strip（精确匹配）
        (users_dir / "bob.txt").write_text(f"  {PASSWORD}  \n", encoding="utf-8")
        assert _check_password("bob", PASSWORD) is True
        assert _check_password("bob", f" {PASSWORD}") is False

    def test_empty_password_file_rejected(self, users_dir):
        (users_dir / "empty.txt").write_text("   \n", encoding="utf-8")
        assert _check_password("empty", "") is False
        assert _check_password("empty", "anything") is False

    def test_password_with_internal_spaces(self, users_dir):
        (users_dir / "sp.txt").write_text("a b c\n", encoding="utf-8")
        assert _check_password("sp", "a b c") is True
        assert _check_password("sp", "abc") is False

    def test_unicode_password(self, users_dir):
        (users_dir / "cn.txt").write_text("密码测试123", encoding="utf-8")
        assert _check_password("cn", "密码测试123") is True

    @pytest.mark.parametrize("bad_name", [
        "", "a", "a" * 33, "张三", "../etc/passwd", "a b", "a/b",
        "alice.txt", "..", "a:b", "a;b",
    ])
    def test_invalid_username_rejected(self, users_dir, bad_name):
        # 非法用户名（过短/过长/中文/路径穿越/非法字符）一律拒绝，
        # 即使构造出同名文件也不能通过
        (users_dir / "张三.txt").write_text(PASSWORD, encoding="utf-8")
        assert _check_password(bad_name, PASSWORD) is False

    @pytest.mark.parametrize("good_name", ["ab", "user_1", "user-2", "A999", "x" * 32])
    def test_valid_username_shape(self, good_name):
        assert USERNAME_RE.match(good_name)


class TestSession:
    """_new_session：Redis 会话 token 的生成与 TTL。"""

    async def test_session_stored_with_ttl(self, fake_redis):
        token = await _new_session("alice")
        assert await fake_redis.get(auth_mod.SESS_PREFIX + token) == "alice"
        ttl = await fake_redis.ttl(auth_mod.SESS_PREFIX + token)
        assert 0 < ttl <= auth_mod.SESSION_TTL

    async def test_tokens_are_unique(self, fake_redis):
        tokens = {await _new_session("alice") for _ in range(20)}
        assert len(tokens) == 20
