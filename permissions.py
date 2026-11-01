import os

_ROLE_ENV_VARS = [
    ("ADMIN_TELEGRAM_ID", "admin"),
    ("WIFE_TELEGRAM_ID", "editor"),
    ("MOM_TELEGRAM_ID", "reader"),
    ("DAD_TELEGRAM_ID", "reader"),
]

# python-dotenv sets a blank "FOO=" line to "", not leaving it unset - so an
# empty-string check (not an is-None check) is what actually skips family
# members who haven't been added yet.
PERMISSIONS = {
    telegram_id: role
    for env_var, role in _ROLE_ENV_VARS
    if (telegram_id := os.environ.get(env_var))
}

ROLES = {
    "admin": {"read", "write", "confirm", "email_approve", "audit"},
    "editor": {"read", "write", "confirm"},
    "reader": {"read"},
}


def get_role(telegram_id) -> str | None:
    return PERMISSIONS.get(str(telegram_id))


def can(telegram_id, capability: str) -> bool:
    role = get_role(telegram_id)
    return role is not None and capability in ROLES.get(role, set())
