from __future__ import annotations

import os
import re
from collections.abc import Collection, Mapping
from typing import Any


_SECRET_ENVIRONMENT_NAME = re.compile(
    r"(?:^|_)(?:API_KEY|TOKEN|SECRET(?:_KEY)?|PASSWORD|CREDENTIALS?|PRIVATE_KEY|ACCESS_KEY)$",
    re.IGNORECASE,
)


def redact_value(value: Any, secrets: Collection[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if len(secret) >= 4:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, secrets) for item in value)
    if isinstance(value, Mapping):
        return {key: redact_value(item, secrets) for key, item in value.items()}
    return value


def safe_child_environment(secrets: Collection[str] = ()) -> dict[str, str]:
    environment = os.environ.copy()
    secret_values = {value for value in secrets if len(value) >= 4}
    for name, value in tuple(environment.items()):
        if _SECRET_ENVIRONMENT_NAME.search(name) or value in secret_values:
            environment.pop(name, None)
    return environment
