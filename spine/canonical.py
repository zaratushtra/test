"""
Canonical serialisation and hashing.

SPINE v2 §9.2: a forecast commits to its inputs by hash. Two implementations that
serialise differently produce different digests for the same forecast, and
verification fails for no reason. So the canonical form is fixed here, once.

Follows RFC 8785 (JSON Canonicalization Scheme) for the subset we use:
  * UTF-8, no BOM
  * object keys sorted by UTF-16 code unit
  * no insignificant whitespace
  * numbers in shortest round-trip form

Deviations, deliberate and enforced rather than tolerated:
  * NaN and Infinity are rejected. They have no JSON representation and their
    presence in a committed payload means an upstream bug.
  * Floats are permitted but discouraged. Probabilities are integers (basis
    points) precisely so the common path never touches float formatting.
    Python's repr has given shortest round-trip since 3.1, which agrees with
    ES6 Number::toString for every value we generate; exotic exponent forms are
    not exercised by this schema.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

GENESIS_HASH = "0" * 64


class CanonicalisationError(ValueError):
    """The payload cannot be canonically serialised."""


def _check(value: Any, path: str = "$") -> None:
    """Reject anything whose canonical form would be ambiguous."""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CanonicalisationError(
                f"{path}: NaN/Infinity cannot be committed to a hash"
            )
    elif isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalisationError(
                    f"{path}: object keys must be strings, got {type(k).__name__}"
                )
            _check(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check(v, f"{path}[{i}]")
    elif not isinstance(value, (str, int, bool, type(None))):
        raise CanonicalisationError(
            f"{path}: {type(value).__name__} has no canonical JSON form"
        )


def canonicalise(payload: Any) -> str:
    """Return the canonical JSON text for a payload."""
    _check(payload)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_hash(payload: Any) -> str:
    """SHA-256 of the canonical form. Lowercase hex."""
    return hashlib.sha256(canonicalise(payload).encode("utf-8")).hexdigest()


def chain_hash(payload: Any, prev_hash: str) -> str:
    """
    Link a payload into a hash chain.

        forecast_hash = SHA256( canonical(content) || prev_hash )

    The payload must already include its own created_at, so backdating a
    committed record breaks verification. Note that this detects tampering
    *within* a chain and does not establish when the chain was created — see
    spine.chain.anchor() and v2 §9.1.
    """
    if not isinstance(prev_hash, str) or len(prev_hash) != 64:
        raise CanonicalisationError(f"prev_hash must be 64 hex chars, got {prev_hash!r}")
    body = canonicalise(payload).encode("utf-8") + prev_hash.encode("ascii")
    return hashlib.sha256(body).hexdigest()
