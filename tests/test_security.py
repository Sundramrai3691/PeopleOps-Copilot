from sql_agent.security import ApiKeyAuthorizer, Principal


def test_api_key_roles_enforce_minimum_permission():
    auth = ApiKeyAuthorizer({
        "read-key": Principal("reader", "viewer"),
        "write-key": Principal("operator", "operator"),
    })
    assert auth.authenticate("write-key", "viewer").name == "operator"
    try:
        auth.authenticate("read-key", "operator")
    except PermissionError as exc:
        assert "operator role required" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("viewer gained operator access")
    for key in (None, "wrong"):
        try:
            auth.authenticate(key, "viewer")
        except PermissionError:
            pass
        else:  # pragma: no cover
            raise AssertionError("invalid key accepted")
