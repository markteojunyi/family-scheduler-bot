"""Tests for the role/capability model."""
import permissions


def test_admin_capabilities():
    assert permissions.ROLES["admin"] >= {"read", "write", "confirm", "email_approve", "audit"}


def test_editor_cannot_approve_email_or_audit():
    assert "email_approve" not in permissions.ROLES["editor"]
    assert "audit" not in permissions.ROLES["editor"]


def test_reader_is_read_only():
    assert permissions.ROLES["reader"] == {"read"}


def test_get_role_known_and_unknown(monkeypatch):
    monkeypatch.setitem(permissions.PERMISSIONS, "42", "editor")
    assert permissions.get_role(42) == "editor"
    assert permissions.get_role("42") == "editor"  # int or str both work
    assert permissions.get_role(123456789) is None


def test_can_checks_capability_not_identity(monkeypatch):
    monkeypatch.setitem(permissions.PERMISSIONS, "42", "editor")
    assert permissions.can(42, "write") is True
    assert permissions.can(42, "audit") is False
    assert permissions.can(999999, "read") is False  # unknown user can't do anything


def test_no_empty_string_landmine():
    # blank env vars ("WIFE_TELEGRAM_ID=") must not create a "" -> role entry
    assert "" not in permissions.PERMISSIONS
    assert None not in permissions.PERMISSIONS
