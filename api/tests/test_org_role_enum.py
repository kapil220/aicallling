from api.enums import Role, role_at_least


def test_admin_at_least_member():
    assert role_at_least(Role.ADMIN, Role.MEMBER) is True


def test_admin_at_least_admin():
    assert role_at_least(Role.ADMIN, Role.ADMIN) is True


def test_member_not_at_least_admin():
    assert role_at_least(Role.MEMBER, Role.ADMIN) is False


def test_member_at_least_member():
    assert role_at_least(Role.MEMBER, Role.MEMBER) is True


def test_role_is_str_enum():
    assert Role.ADMIN.value == "admin"
    assert Role.MEMBER == "member"
