# Copyright 2026 Open Eye Development
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""I/O wrappers: load a source OpenDocument, return its ``content.xml`` to
the transformer, then re-pack the multi-file ``.odt`` zip (or write the flat
``.fodt`` XML).

The ``.odt`` format is a ZIP archive whose members include at minimum:
``mimetype`` (the file type identifier), ``META-INF/manifest.xml``,
``content.xml``, and typically ``styles.xml``, ``meta.xml``. The ``mimetype``
member must be stored *uncompressed* and *first*; we preserve that
convention when re-zipping.
"""
from __future__ import annotations

import io
import zipfile


def _is_odt(src_format: str) -> bool:
    return src_format.lower() in ("odt", "ods", "odp", "fodt", "fods", "fodp")


def load(data: bytes, src_format: str) -> tuple[bytes, str | None, list[tuple[str, bytes]]]:
    """Decompose a source OpenDocument into its ``content.xml`` plus extras.

    :returns: ``(content_xml_bytes, mimetype, extra_files)``.
        For ``.odt``/``.ods``/``.odp`` (zipped format), ``extra_files`` is the
        list of ``(name, data)`` pairs for every other zip member except the
        ``content.xml`` member and the ``mimetype`` member (which is returned
        separately). For ``.fodt``/``.fods``/``.fodp``, the entire document is
        the XML, ``mimetype`` is ``None`` and ``extra_files`` is empty.
    """
    src_format = src_format.lower()
    if src_format.startswith("fod"):
        # Flat XML — the whole file is the content.xml-equivalent.
        return data, None, []
    if src_format not in ("odt", "ods", "odp"):
        raise ValueError(f"Unsupported source format: {src_format!r}")

    bio = io.BytesIO(data)
    with zipfile.ZipFile(bio, "r") as zf:
        names = zf.namelist()
        mimetype = None
        content_xml = None
        extras: list[tuple[str, bytes]] = []
        for name in names:
            raw = zf.read(name)
            if name == "mimetype":
                mimetype = raw.decode("ascii", "replace")
            elif name == "content.xml":
                content_xml = raw
            else:
                extras.append((name, raw))
    if content_xml is None:
        raise ValueError(
            "OpenDocument zip has no content.xml member; is this a valid .odt?"
        )
    return content_xml, mimetype, extras


def dump(
    content_xml: bytes,
    src_format: str,
    mimetype: str | None,
    extras: list[tuple[str, bytes]],
) -> bytes:
    """Re-serialize the modified OpenDocument.

    See :func:`load` for the argument shapes. For zipped formats, rewrites
    the ``.odt``/``.ods``/``.odp`` archive preserving member order (mimetype
    uncompressed first if present, then every other member).
    """
    src_format = src_format.lower()
    if src_format.startswith("fod"):
        # Flat XML — modifiers already operated on this bytes object.
        return content_xml

    if src_format not in ("odt", "ods", "odp"):
        raise ValueError(f"Unsupported target format: {src_format!r}")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if mimetype is not None:
            # Mimetype MUST be stored uncompressed and first.
            zi = zipfile.ZipInfo("mimetype")
            zi.compress_type = zipfile.ZIP_STORED
            zf.writestr(zi, mimetype.encode("ascii", "replace"))
        # Preserve author-supplied files in their original order, then append
        # content.xml.
        for name, raw in extras:
            zi = zipfile.ZipInfo(name)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, raw)
        zi = zipfile.ZipInfo("content.xml")
        zi.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi, content_xml)
    return out.getvalue()
