"""
Handling text and links that came from the open internet.

§11.3: "Retrieved news and documents are untrusted throughout. They must never
be able to instruct the system to execute tools, expose secrets or alter trading
controls." There is no LLM in this pipeline yet, so the prompt-injection half of
that is not live — but the *other* half already is, because this project ingests
open-internet text into a database and then adjudicates it.

Two hazards, handled differently, and the difference is the point.

**Display-altering characters.** A bidirectional override makes text render in
an order other than the one it is stored in, so a claim that hashes one way can
read another way to the human adjudicating it. In a project whose entire premise
is "a verifiable record of what was asserted", that is not a cosmetic problem:
`RAISE` and `‮` + `ESIAR` are different bytes, the same picture, and one
signature.

The split:

  * **Source text is preserved and flagged.** If a publisher really did emit a
    bidi override, that is a fact about the publisher and destroying it destroys
    evidence. The item is stored verbatim and the finding travels with it.
  * **Our own text is refused.** A `claims.assertion` is a summary *we* wrote and
    a human will adjudicate. Nothing legitimate needs an invisible reordering
    control, so it is rejected rather than flagged.

**Links.** A feed supplies `url` freely. `javascript:`, `data:` and `file:` are
not references to an article; they are instructions waiting for something to
follow them. This project has no renderer today and will grow one, and a stored
hazard is a hazard on the day something reads it. The scheme is checked at
ingest, the unsafe value is dropped rather than stored, and the rejection is
recorded — the article is still evidence even when its link is not usable.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

# The only schemes that denote a document you could go and read.
SAFE_URL_SCHEMES = frozenset({"http", "https"})

# Bidirectional formatting controls. These reorder rendered text without
# changing the bytes, which is the whole attack: what is hashed and what is read
# are different things. (Trojan Source, CVE-2021-42574.)
BIDI_CONTROLS = frozenset(
    "‪‫‬‭‮"          # LRE RLE PDF LRO RLO
    "⁦⁧⁨⁩"                # LRI RLI FSI PDI
    "‎‏؜"                      # LRM RLM ALM
)

# Zero-width characters: invisible, and they break naive equality and search.
ZERO_WIDTH = frozenset("​‌‍﻿")

# C0 controls other than the three that legitimately occur in text.
_ALLOWED_C0 = frozenset("\t\n\r")


class UntrustedInputError(ValueError):
    """Text from an untrusted source would misrepresent itself if accepted."""


def scan_text(text: str) -> list[str]:
    """
    Findings about text that would render differently than it is stored.

    Returns a list of human-readable findings, empty when there are none. It
    reports rather than judging, because the right response differs by field:
    evidence is kept and flagged, our own prose is refused.
    """
    if not isinstance(text, str):
        return ["not a string"]
    findings = []

    bidi = sorted({c for c in text if c in BIDI_CONTROLS})
    if bidi:
        names = ", ".join(f"U+{ord(c):04X}" for c in bidi)
        findings.append(
            f"bidirectional control(s) {names}: rendered order differs from "
            "stored order, so what is hashed is not what a reader sees")

    zw = sorted({c for c in text if c in ZERO_WIDTH})
    if zw:
        findings.append(
            "zero-width character(s) "
            + ", ".join(f"U+{ord(c):04X}" for c in zw)
            + ": invisible, and they defeat equality and search")

    nul = "\x00" in text
    if nul:
        findings.append(
            "NUL byte: truncates in most C-backed consumers, so the stored text "
            "and the displayed text diverge")

    other = sorted({c for c in text
                    if unicodedata.category(c) == "Cc"
                    and c not in _ALLOWED_C0 and c != "\x00"})
    if other:
        findings.append(
            "control character(s) "
            + ", ".join(f"U+{ord(c):04X}" for c in other))
    return findings


def is_display_safe(text: str) -> bool:
    return not scan_text(text)


def require_display_safe(text: str, field: str = "text") -> str:
    """
    Refuse text that would render differently than it is stored.

    For our own prose only. Applying this to source material would silently
    discard evidence about what a publisher actually emitted.
    """
    findings = scan_text(text)
    if findings:
        raise UntrustedInputError(
            f"{field} contains {findings[0]}. This is our own text, not a "
            "source's, and nothing legitimate needs an invisible reordering "
            "control — a claim that hashes one way and reads another defeats "
            "the point of recording it")
    return text


def describe(text: str) -> str | None:
    """One line summarising findings, or None. Stored alongside flagged items."""
    findings = scan_text(text)
    return "; ".join(findings) if findings else None


def check_url(url: str | None) -> tuple[str | None, str | None]:
    """
    Return `(safe_url, rejection_reason)`.

    An unsafe URL is dropped rather than stored. The article behind it is still
    evidence; the link is not something anything here should follow, and a
    stored hazard is a hazard on the day something reads it rather than the day
    it arrives.
    """
    if url is None or not str(url).strip():
        return None, None
    raw = str(url).strip()

    if any(c in raw for c in BIDI_CONTROLS | ZERO_WIDTH) or "\x00" in raw:
        return None, "url contains invisible or reordering characters"
    if re.search(r"[\x00-\x1f\x7f]", raw):
        return None, "url contains control characters"

    try:
        parts = urlsplit(raw)
    except ValueError as e:
        return None, f"url is unparseable: {e}"

    scheme = parts.scheme.lower()
    if not scheme:
        return None, "url has no scheme; a bare string is not a reference"
    if scheme not in SAFE_URL_SCHEMES:
        return None, (
            f"scheme {scheme!r} is not one of {sorted(SAFE_URL_SCHEMES)}: this "
            "is an instruction waiting to be followed, not a document")
    if not parts.netloc:
        return None, "url has no host"
    return raw, None
