"""Host-side explode renderer for ``--extract``.

EXTRACT is not a second verb: a generated filter still returns ordinary SELECT
records (``{"path": ..., ...}``). When a record carries a list-valued extra field
(e.g. ``todos: [{"line": 42, "text": "..."}]``) ``--extract`` streams one
``path[:line]<TAB><payload>`` line per element of that list -- the grep-shaped,
match-grained output that file-grained rendering can't express.

These helpers are pure (no I/O) so the selection and formatting rules are testable
in isolation; ``cli._emit`` drives them and picks the line terminator.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

# Element fields that the renderer treats specially rather than as plain key=value:
# line/col build the ``path:line:col`` prefix; text is shown as a bare payload.
_PREFIX_FIELDS = ("line", "col")
_TEXT_FIELD = "text"


def _list_fields(record: dict[str, Any]) -> list[str]:
    """Names of the record's extra fields whose value is a list (path excluded)."""
    return [key for key, value in record.items() if key != "path" and isinstance(value, list)]


def select_list_field(record: dict[str, Any], field: str | None) -> str | None:
    """Pick which list-valued field of ``record`` to explode.

    With ``field`` given, that field is used when it is a list on this record (else
    the record has nothing to explode under that name and ``None`` is returned, so a
    mixed result set still prints). Otherwise the sole list field is chosen; two or
    more raise a ``ValueError`` naming the candidates rather than guessing; none
    returns ``None`` (the record degrades to a single path line).
    """
    if field is not None:
        return field if isinstance(record.get(field), list) else None
    candidates = _list_fields(record)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None
    names = ", ".join(sorted(candidates))
    raise ValueError(
        f"record for {record['path']!r} has multiple list-valued fields ({names}); "
        "use --extract-field NAME to choose which one to explode."
    )


def _scalar_extras(record: dict[str, Any], *, skip: str | None = None) -> str:
    """``key=value`` for the record's non-list extra fields, comma-joined (or "")."""
    parts = [
        f"{key}={value}"
        for key, value in record.items()
        if key != "path" and key != skip and not isinstance(value, list)
    ]
    return ", ".join(parts)


def render_extract_row(host_path: str, element: Any) -> str:
    """Format one exploded element as a ``path[:line[:col]]<TAB><payload>`` line.

    A dict element contributes ``line``/``col`` to the path prefix (only when
    present), shows ``text`` as a bare value, and renders every other field as
    ``key=value``. A scalar element (e.g. a bare URL string) is shown verbatim as
    the payload. When there is no payload, just the (possibly line-anchored) path
    is returned.
    """
    if not isinstance(element, dict):
        return f"{host_path}\t{element}"

    prefix = host_path
    for name in _PREFIX_FIELDS:
        value = element.get(name)
        if value is None:
            break
        prefix = f"{prefix}:{value}"

    payload_parts: list[str] = []
    text = element.get(_TEXT_FIELD)
    if text is not None:
        payload_parts.append(str(text))
    for key, value in element.items():
        if key in _PREFIX_FIELDS or key == _TEXT_FIELD or key == "path":
            continue
        payload_parts.append(f"{key}={value}")

    payload = ", ".join(payload_parts)
    return f"{prefix}\t{payload}" if payload else prefix


def iter_extract_rows(records: list[dict[str, Any]], field: str | None) -> Iterator[str]:
    """Yield one formatted line per exploded element across all records.

    Each record's list field is chosen by :func:`select_list_field`; its elements
    are rendered by :func:`render_extract_row`. A record with no list field to
    explode degrades to a single line (path plus any scalar extras), so heterogeneous
    result sets still print. Lines carry no terminator -- the caller joins them with
    ``\\n`` or, under ``--print0``, ``\\0``.
    """
    for record in records:
        host_path = record["path"]
        chosen = select_list_field(record, field)
        if chosen is None:
            extras = _scalar_extras(record)
            yield f"{host_path}\t{extras}" if extras else host_path
            continue
        for element in record[chosen]:
            yield render_extract_row(host_path, element)
