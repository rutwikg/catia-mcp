"""Viewport capture, with the format enumeration discovered rather than assumed.

``Viewer3D.CaptureToFile(format, path)`` takes a ``CatCaptureFormat`` integer.
The numbering has moved between releases, and a wrong value fails in the worst
possible way: CATIA happily writes, say, a CGM file to ``shot.png`` and returns
success. Rather than trust a table, we write one throwaway capture per
candidate integer and read the file's magic bytes to learn what that integer
actually means on *this* installation. The answer is cached for the session.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from catia_mcp.core import constants, errors

logger = logging.getLogger("catia_mcp.capture")

# Preference order when the caller does not care: lossless first, then JPEG.
PREFERRED = ("png", "bmp", "tiff", "jpeg")


def _sniff(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    if not head:
        return None
    for name, magic in constants.FILE_MAGIC.items():
        if head.startswith(magic):
            return "tiff" if name.startswith("tiff") else name
    if head.lstrip()[:6] in (b"BEGMF", b"BEGMF;"):
        return "cgm"
    return None


def discover_formats(viewer: Any) -> dict[str, int]:
    """Map format name -> the integer this CATIA wants for it."""
    found: dict[str, int] = {}
    directory = tempfile.mkdtemp(prefix="catia_mcp_probe_")
    try:
        for _documented_name, candidate in constants.CAPTURE_FORMAT_ORDER:
            probe = os.path.join(directory, "probe_%d.bin" % candidate)
            try:
                viewer.CaptureToFile(candidate, probe)
            except Exception as exc:
                if errors.is_retryable(exc) or errors.is_dead(exc):
                    raise
                logger.debug("Capture format %d rejected: %s", candidate, exc)
                continue
            actual = _sniff(probe)
            if actual and actual not in found:
                found[actual] = candidate
                logger.debug("Capture format %d writes %s", candidate, actual)
    finally:
        try:
            for entry in os.listdir(directory):
                os.unlink(os.path.join(directory, entry))
            os.rmdir(directory)
        except OSError:
            pass
    return found


class CaptureFormats:
    """Session-lifetime cache of the probe result."""

    def __init__(self) -> None:
        self._map: dict[str, int] | None = None

    def reset(self) -> None:
        self._map = None

    def known(self) -> dict[str, int] | None:
        return dict(self._map) if self._map else None

    def get(self, viewer: Any, wanted: str | None = None) -> tuple[str, int]:
        """Return ``(format_name, catia_enum_value)`` for the wanted format."""
        if self._map is None:
            self._map = discover_formats(viewer)
            if not self._map:
                # Nothing recognisable came back; fall back to the documented
                # table so we at least produce a file.
                logger.warning(
                    "Capture-format probing produced no recognisable images; "
                    "falling back to the documented CatCaptureFormat values."
                )
                self._map = {
                    name: value
                    for name, value in constants.CAPTURE_FORMAT_ORDER
                    if name in ("png", "bmp", "jpeg", "tiff")
                }

        if wanted:
            key = wanted.lower().lstrip(".")
            if key in ("jpg",):
                key = "jpeg"
            if key in ("tif",):
                key = "tiff"
            if key in self._map:
                return key, self._map[key]
            raise errors.UnsupportedCapabilityError(
                "This CATIA cannot capture %s. Available: %s"
                % (wanted, ", ".join(sorted(self._map)) or "none")
            )

        for name in PREFERRED:
            if name in self._map:
                return name, self._map[name]
        name = next(iter(self._map))
        return name, self._map[name]


CAPTURE_FORMATS = CaptureFormats()
