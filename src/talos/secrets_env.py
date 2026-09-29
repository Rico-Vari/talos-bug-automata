#!/usr/bin/env python3
"""
Secret resolution for the event harness.

An absolute, greppable rule: **a value in config.yaml is never a secret**.
config.yaml stores the *name* of the environment variable; the value lives in
~/.orchestrator/secrets.env (mode 0600), loaded by systemd through
EnvironmentFile. That way "is this field a secret?" is never ambiguous.
"""
from __future__ import annotations

import logging
import os

SECRETS_FILE = "~/.orchestrator/secrets.env"

# Marker of the placeholders the initial setup leaves behind. A secret that
# still carries it is not configured, and treating it as valid would produce
# a confusing 401 instead of a clear error at startup.
PLACEHOLDER_SUFFIX = "_REPLACE_ME"

logger = logging.getLogger("talos")
_warned_literal_telegram = False


class MissingSecret(RuntimeError):
    pass


def resolve_secret(cfg: dict, key: str, *, required: bool = True) -> str:
    """Returns the value of the secret that `cfg['secrets'][key]` names.

    `key` is the key in the `secrets:` block (e.g. 'github_webhook_secret_env'),
    not the variable's name. Raises MissingSecret naming the exact missing
    variable, so the error says which line to add to secrets.env.
    """
    var = (cfg.get("secrets") or {}).get(key, "")
    if not var:
        if required:
            raise MissingSecret(
                f"config.yaml does not define secrets.{key} (the env var name)"
            )
        return ""

    value = os.environ.get(var, "")
    if not value or value.endswith(PLACEHOLDER_SUFFIX):
        if required:
            raise MissingSecret(
                f"The variable {var} is not set or still has the placeholder. "
                f"Edit {SECRETS_FILE} and restart the service."
            )
        return ""
    return value


def telegram_token(cfg: dict) -> str:
    """The Telegram bot token, or '' when Telegram is not configured.

    `secrets.telegram_bot_token_env` names the variable that holds it
    (TELEGRAM_BOT_TOKEN when the key is missing). A literal
    `telegram_bot_token` in config.yaml still works, so an existing install
    keeps notifying, but it breaks the rule above and gets a warning.
    """
    global _warned_literal_telegram
    named = (cfg.get("secrets") or {}).get("telegram_bot_token_env", "")
    var = named or "TELEGRAM_BOT_TOKEN"
    value = os.environ.get(var, "")
    if value and not value.endswith(PLACEHOLDER_SUFFIX):
        return value
    literal = str(cfg.get("telegram_bot_token") or "")
    if not _warned_literal_telegram and (literal or named):
        _warned_literal_telegram = True
        if named:
            logger.warning("secrets.telegram_bot_token_env names %s, which is not set in "
                           "%s%s", var, SECRETS_FILE,
                           ": using the literal telegram_bot_token" if literal else
                           ": Telegram notifications are off")
        else:
            logger.warning(
                "telegram_bot_token is a literal in config.yaml: move it to %s as %s "
                "and set secrets.telegram_bot_token_env", SECRETS_FILE, var,
            )
    return literal


def load_dotenv(path: str = SECRETS_FILE) -> int:
    """Loads secrets.env into the environment for manual runs outside systemd.

    Does not overwrite variables already set: systemd wins over the file.
    Returns how many variables it loaded.
    """
    from pathlib import Path

    f = Path(path).expanduser()
    if not f.exists():
        return 0
    loaded = 0
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value
            loaded += 1
    return loaded
