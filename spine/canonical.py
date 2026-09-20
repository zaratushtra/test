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

Floats are permitted but discouraged. Probabilities are integers (basis points)
precisely so the common path never touches float formatting.

**Numbers follow ES6 `Number::toString`, which is what RFC 8785 requires — not
Python's `repr`.** An earlier version used `repr` on the reasoning that it gives
the shortest round-trip form and "agrees with ES6 for every value we generate".
The first half is true; the second was not. `repr(1.0)` is `'1.0'` and ES6 gives
`'1'`, and the parameter snapshot committed by *every* forecast contains `1.0`,
`900.0` and `0.25`. So the deviation was not exotic, it was universal.

That matters because the point of content-addressing here is that **someone else
can check it**. §9.1 anchors the chain externally so pre-registration is a claim
a third party need believe; a third party reimplementing RFC 8785 would have
computed different digests for the same data and concluded the record was
forged.

**Depth is bounded.** `_check` and `_serialise` are both recursive, and a
payload nested a few hundred deep — or one containing a reference to itself —
exhausted the interpreter stack and raised `RecursionError` straight out of
`content_hash`. That is the same shape of failure as the narrow `except` in
`venue.snapshot_books`: an exception nobody named escaping the module that was
supposed to have decided what it means. A payload that deep is a bug or an
attack, never a forecast, so it is refused by name.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

GENESIS_HASH = "0" * 64

# Deepest nesting a payload may have. The deepest thing this project actually
# commits is a manifest holding a parameter snapshot, at four levels; sixty-four
# leaves three orders of magnitude of headroom and still refuses long before
# CPython's own recursion limit (1000 by default, and each level here costs two
# frames). A self-referential structure has no depth at all, so it trips this
# too — which is the intended answer, since it has no canonical form either.
MAX_DEPTH = 64


class CanonicalisationError(ValueError):
    """The payload cannot be canonically serialised."""


def _check(value: Any, path: str = "$", depth: int = 0) -> None:
    """Reject anything whose canonical form would be ambiguous."""
    if depth > MAX_DEPTH:
        # The path is elided in the middle: at this depth it is sixty-odd
        # repetitions of the same key, and the two ends are the only part that
        # tells a reader where to look.
        shown = path if len(path) <= 64 else f"{path[:30]}...{path[-30:]}"
        raise CanonicalisationError(
            f"{shown}: nested deeper than {MAX_DEPTH} levels, or contains a "
            "reference to itself. Either way it has no canonical form; "
            "serialising it would exhaust the stack rather than refuse"
        )
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
            _check(v, f"{path}.{k}", depth + 1)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check(v, f"{path}[{i}]", depth + 1)
    elif not isinstance(value, (str, int, bool, type(None))):
        raise CanonicalisationError(
            f"{path}: {type(value).__name__} has no canonical JSON form"
        )


def _es6_number(x: float) -> str:
    """
    A float as ES6 `Number::toString` renders it, which is RFC 8785's rule.

    Python's `repr` already produces the shortest round-tripping digit string,
    so the digits are right; only the *formatting* differs. The thresholds below
    are the ES6 ones: plain decimal while the exponent sits in a middling range,
    scientific outside it, no trailing `.0`, and no `-0`.
    """
    if x == 0:                       # covers -0.0, which ES6 renders as "0"
        return "0"
    if x < 0:
        return "-" + _es6_number(-x)

    # Shortest round-trip digits and a decimal exponent, from repr.
    r = repr(x)
    if "e" in r or "E" in r:
        mantissa, exp = r.lower().split("e")
        exp = int(exp)
    else:
        mantissa, exp = r, 0
    if "." in mantissa:
        whole, frac = mantissa.split(".")
    else:
        whole, frac = mantissa, ""
    digits = (whole + frac).lstrip("0") or "0"
    # n is the position of the decimal point relative to the digit string:
    # value == 0.<digits> * 10**n
    n = len(whole.lstrip("0")) + exp
    if whole.lstrip("0") == "":                  # 0.00123 style
        n = exp - (len(frac) - len(frac.lstrip("0")))
    digits = digits.rstrip("0") or "0"
    k = len(digits)

    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    e = n - 1
    sign = "+" if e >= 0 else "-"
    head = digits[0] if k == 1 else digits[0] + "." + digits[1:]
    return f"{head}e{sign}{abs(e)}"


# RFC 8785's escaping: the short forms where they exist, \u00XX otherwise, and
# nothing else escaped. Notably '/' is NOT escaped and non-ASCII is emitted
# literally as UTF-8.
_ESCAPES = {
    '"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f",
    "\n": "\\n", "\r": "\\r", "\t": "\\t",
}


def _es6_string(value: str) -> str:
    out = ['"']
    for ch in value:
        esc = _ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ch < "\x20":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_key(key: str) -> tuple[int, ...]:
    """
    Sort key giving RFC 8785's ordering: by UTF-16 code unit, not code point.

    These differ above the BMP. U+FFFD sorts *after* U+10000 by code point and
    *before* it by code unit, because an astral character encodes as a surrogate
    pair beginning 0xD800-0xDBFF. Sorting by code point would put such keys in
    an order no conforming implementation agrees with, and the digest would
    differ for data nobody would think to look at.
    """
    return tuple(key.encode("utf-16-be")[i] << 8 | key.encode("utf-16-be")[i + 1]
                 for i in range(0, len(key.encode("utf-16-be")), 2))


def _serialise(value: Any) -> str:
    """The canonical form, written directly.

    `json.dumps` cannot be used for the number rule: its encoder calls
    `float.__repr__` explicitly, so a float subclass overriding `__repr__` is
    ignored, and there is no hook for number formatting. Since canonicalisation
    is exactly the job of controlling every byte, writing it out is the honest
    implementation rather than a workaround.
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float):
        return _es6_number(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _es6_string(value)
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: _utf16_key(kv[0]))
        return "{" + ",".join(f"{_es6_string(k)}:{_serialise(v)}"
                              for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_serialise(v) for v in value) + "]"
    raise CanonicalisationError(
        f"{type(value).__name__} has no canonical JSON form")


def canonicalise(payload: Any) -> str:
    """Return the canonical JSON text for a payload.

    `_check` runs first and refuses anything past `MAX_DEPTH`, so `_serialise`
    — recursive and unguarded — is only ever reached on a payload already known
    to be shallow enough. The guard lives in one of the two rather than both
    because two depth limits are two things that can drift apart.
    """
    _check(payload)
    return _serialise(payload)


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
