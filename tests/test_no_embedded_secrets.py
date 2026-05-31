# Copyright bengr. Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at http://www.apache.org/licenses/LICENSE-2.0
"""Guardrail: the redistributable source must contain no user-specific data.

This enforces the project principle that credentials, account IDs, ARNs, and secrets are supplied
only at runtime and are never hardcoded or committed. It scans the shipped package and the IAM
policy documents (not docs/tests, which legitimately contain placeholders and patterns).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [ROOT / "workspaces_euc_mcp_server", ROOT / "iam"]

# (label, pattern). Patterns target real-credential / account-specific shapes.
FORBIDDEN = [
    ("aws access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws secret assignment", re.compile(r"aws_secret_access_key\s*=\s*['\"]")),
    ("12-digit account id", re.compile(r"\b\d{12}\b")),
    ("account-scoped arn", re.compile(r"arn:aws:[a-z0-9-]*:[a-z0-9-]*:\d{12}:")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def _files() -> list[Path]:
    files: list[Path] = []
    for base in SCAN_DIRS:
        files.extend(p for p in base.rglob("*.py"))
        files.extend(p for p in base.rglob("*.json"))
    return files


def test_no_embedded_credentials_or_account_data():
    offenders: list[str] = []
    for path in _files():
        text = path.read_text(encoding="utf-8")
        for label, pattern in FORBIDDEN:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)}: {label}")
    assert not offenders, "Embedded user-specific data found:\n" + "\n".join(offenders)
