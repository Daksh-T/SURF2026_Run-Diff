"""Seal/unseal an exported assignment file against casual reading.

Sealing does NOT add security against a determined student — it's gzip+base64 of the
same JSON, reversible with one line of Python. What it defeats is a curious student
opening the file in a text editor (or VS Code) and reading the per-seed baked gold
results, which is a real (if bounded) information leak documented in README.md's
"What an assignment file can and cannot leak". A reader of the sealed envelope sees only
a format tag, a note, and a base64 blob.

The gold SQL itself was already eliminated at publish time (`store.assert_student_safe`);
sealing is a second, much weaker layer on top of that, aimed at the casual case.
"""
from __future__ import annotations

import base64
import binascii
import gzip
import json
import zlib

FORMAT = "rundiff-assignment-v2"
ENCODING = "gzip+b64"
NOTE = ("Sealed practice assignment for the Run·Diff tutor. Not human-readable by "
        "design — load it in the app.")


def seal(obj: dict) -> dict:
    """Wrap `obj` (a plain rundiff-assignment-v1 dict) in a sealed v2 envelope."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(canonical)
    payload = base64.b64encode(compressed).decode("ascii")
    return {
        "format": FORMAT,
        "sealed": True,
        "encoding": ENCODING,
        "note": NOTE,
        "payload": payload,
    }


def unseal(envelope: dict) -> dict:
    """Reverse `seal`. Raises ValueError with a helpful message on any malformed input."""
    if not isinstance(envelope, dict):
        raise ValueError("sealed assignment must be a JSON object")
    if envelope.get("format") != FORMAT:
        raise ValueError(
            f"unrecognized sealed assignment format {envelope.get('format')!r} "
            f"(expected {FORMAT!r})")
    if envelope.get("encoding") != ENCODING:
        raise ValueError(
            f"unsupported sealed assignment encoding {envelope.get('encoding')!r} "
            f"(expected {ENCODING!r})")
    payload = envelope.get("payload")
    if not isinstance(payload, str):
        raise ValueError("sealed assignment missing 'payload' string")
    try:
        compressed = base64.b64decode(payload, validate=True)
    except binascii.Error as e:
        raise ValueError(f"sealed assignment payload is not valid base64: {e}")
    try:
        canonical = gzip.decompress(compressed)
    except (OSError, zlib.error, EOFError) as e:
        raise ValueError(f"sealed assignment payload is not valid gzip: {e}")
    try:
        return json.loads(canonical)
    except json.JSONDecodeError as e:
        raise ValueError(f"sealed assignment payload did not decode to JSON: {e}")
