"""CATIA automation enumerations and format tables.

CATIA's enumerated constants are not exposed through the automation interface
itself, so any Python client has to hard-code the integers. A handful of them
have drifted between releases, and a wrong value fails *silently* (you get a
minimal-propagation fillet instead of a tangency one, or a CGM file with a
``.jpg`` extension).

Two mitigations live here:

* Every value can be overridden at startup through a JSON file pointed at by
  ``CATIA_MCP_CONSTANTS``, so a site with an unusual release can correct one
  number without patching code.
* Values whose mistakes are silent - currently the capture formats - are
  *probed* at runtime instead of trusted. See ``catia_mcp.core.capture``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("catia_mcp.constants")

# ── Document creation keys accepted by Documents.Add() ───────────────────────
DOC_TYPE_PART = "Part"
DOC_TYPE_PRODUCT = "Product"
DOC_TYPE_DRAWING = "Drawing"
DOC_TYPE_PROCESS = "Process"

DOCUMENT_TYPES = {
    "part": DOC_TYPE_PART,
    "product": DOC_TYPE_PRODUCT,
    "assembly": DOC_TYPE_PRODUCT,
    "drawing": DOC_TYPE_DRAWING,
    "process": DOC_TYPE_PROCESS,
}

# ── Enumerations ─────────────────────────────────────────────────────────────
_DEFAULTS: dict[str, int] = {
    # CatFilletEdgePropagation
    "catTangencyFilletEdgePropagation": 1,
    "catMinimalFilletEdgePropagation": 2,
    # CatFilletOrientation
    "catNoReverseFilletOrientation": 1,
    "catReverseFilletOrientation": 2,
    # CatChamferPropagation
    "catTangencyChamfer": 1,
    "catMinimalChamfer": 2,
    # CatChamferMode
    "catTwoLengthChamfer": 1,
    "catLengthAngleChamfer": 2,
    # CatChamferOrientation
    "catNoReverseChamfer": 1,
    "catReverseChamfer": 2,
    # CatPrismOrientation (Pad/Pocket DirectionOrientation)
    "catRegularOrientation": 0,
    "catInverseOrientation": 1,
    # CatPrismLimitType / CatLimitMode (FirstLimit.LimitMode)
    "catOffsetLimit": 0,
    "catUpToNextLimit": 1,
    "catUpToLastLimit": 2,
    "catUpToPlaneLimit": 3,
    "catUpToSurfaceLimit": 4,
    # CatTransformationType
    "catRectangularRepartition": 0,
    # CatSplitSide / CatHybridShapeSplitSide
    "catHybridShapeSplitPositiveSide": 1,
    "catHybridShapeSplitNegativeSide": 0,
    # CatHoleType
    "catSimpleHole": 0,
    "catTaperedHole": 1,
    "catCounterboredHole": 2,
    "catCountersunkHole": 3,
    "catCounterdrilledHole": 4,
    # CatHoleBottomType
    "catFlatHoleBottom": 0,
    "catVHoleBottom": 1,
    "catTrimmedHoleBottom": 2,
    # CatHoleAnchorMode
    "catExtremePointHoleAnchor": 0,
    "catMiddlePointHoleAnchor": 1,
    # CatDraftMode
    "catStandardDraftMode": 0,
    "catReflectKeepFaceDraftMode": 1,
    "catReflectKeepEdgeDraftMode": 2,
    # CatDraftMultiselectionMode
    "catNoneDraftMultiselectionMode": 0,
    # CatConstraintType (assembly + sketch)
    "catCstTypeReference": 1,
    "catCstTypeDistance": 2,
    "catCstTypeOn": 3,
    "catCstTypeConcentricity": 4,
    "catCstTypeTangency": 5,
    "catCstTypeLength": 6,
    "catCstTypeAngle": 7,
    "catCstTypePlanarAngle": 8,
    "catCstTypeParallelism": 9,
    "catCstTypeAnnotationText": 10,
    "catCstTypeHorizontality": 11,
    "catCstTypeVerticality": 12,
    "catCstTypePerpendicularity": 13,
    "catCstTypeRadius": 14,
    "catCstTypeSymmetry": 15,
    "catCstTypeMajorRadius": 16,
    "catCstTypeMinorRadius": 17,
    "catCstTypeSurfContact": 18,
    "catCstTypeChamfer": 19,
    "catCstTypeReflection": 20,
    # CatConstraintMode
    "catCstModeDrivingDimension": 0,
    "catCstModeDrivenDimension": 1,
    # CatSketchPositionMode
    "catSketchPositionModeImplicit": 0,
    # CatWorkbenchMode
    "catWorkbenchModeSketch": 0,
    # CatRenderingMode / view rendering styles are set through Viewer3D.
}

_OVERRIDE_PATH = os.environ.get("CATIA_MCP_CONSTANTS", "")
_OVERRIDES: dict[str, int] = {}
if _OVERRIDE_PATH:
    try:
        with open(_OVERRIDE_PATH, encoding="utf-8") as handle:
            raw = json.load(handle)
        ignored: list[str] = []
        for key, value in raw.items():
            # Skip comment keys and anything that is not an integer, so one bad
            # line does not discard the whole file.
            if key.startswith("_") or isinstance(value, bool):
                ignored.append(key)
                continue
            try:
                _OVERRIDES[str(key)] = int(value)
            except (TypeError, ValueError):
                ignored.append(key)
        logger.info("Loaded %d constant override(s) from %s", len(_OVERRIDES), _OVERRIDE_PATH)
        if ignored:
            logger.info("Ignored non-integer entries: %s", ", ".join(ignored))
    except Exception as exc:  # pragma: no cover - operator misconfiguration
        logger.warning("Could not read CATIA_MCP_CONSTANTS=%s: %s", _OVERRIDE_PATH, exc)


def const(name: str) -> int:
    """Look up a CATIA enumeration value, honouring site overrides."""
    if name in _OVERRIDES:
        return _OVERRIDES[name]
    try:
        return _DEFAULTS[name]
    except KeyError:  # pragma: no cover - programming error
        raise KeyError("Unknown CATIA constant: %s" % name) from None


def all_constants() -> dict[str, int]:
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in _OVERRIDES.items() if k in _DEFAULTS})
    return merged


def override_source() -> str | None:
    return _OVERRIDE_PATH or None


# ── Capture formats (probed at runtime, see core/capture.py) ─────────────────
# Ordering matches the CatCaptureFormat enumeration as documented for V5R21.
CAPTURE_FORMAT_ORDER = [
    ("tiff", 0),
    ("tiff_rgb", 1),
    ("bmp", 2),
    ("cgm", 3),
    ("emf", 4),
    ("jpeg", 5),
    ("png", 6),
    ("tiff_rgba", 7),
]

# File magic used to verify what CATIA actually wrote during probing.
FILE_MAGIC = {
    "png": b"\x89PNG\r\n\x1a\n",
    "jpeg": b"\xff\xd8\xff",
    "bmp": b"BM",
    "tiff_le": b"II*\x00",
    "tiff_be": b"MM\x00*",
}

# ── ExportData / SaveAs format keys ──────────────────────────────────────────
# The string handed to Document.ExportData() is the CATIA format key, which for
# almost every format equals the file extension.
EXPORT_FORMATS: dict[str, dict[str, Any]] = {
    "step": {"key": "stp", "ext": ".stp", "kinds": ["part", "product"], "label": "STEP AP203/214"},
    "stp": {"key": "stp", "ext": ".stp", "kinds": ["part", "product"], "label": "STEP"},
    "iges": {"key": "igs", "ext": ".igs", "kinds": ["part"], "label": "IGES"},
    "igs": {"key": "igs", "ext": ".igs", "kinds": ["part"], "label": "IGES"},
    "stl": {"key": "stl", "ext": ".stl", "kinds": ["part"], "label": "STL mesh"},
    "vrml": {"key": "wrl", "ext": ".wrl", "kinds": ["part", "product"], "label": "VRML"},
    "wrl": {"key": "wrl", "ext": ".wrl", "kinds": ["part", "product"], "label": "VRML"},
    "cgr": {"key": "cgr", "ext": ".cgr", "kinds": ["part", "product"], "label": "CATIA graphic"},
    "3dxml": {"key": "3dxml", "ext": ".3dxml", "kinds": ["part", "product"], "label": "3D XML"},
    "model": {"key": "model", "ext": ".model", "kinds": ["part"], "label": "CATIA V4 model"},
    "dxf": {"key": "dxf", "ext": ".dxf", "kinds": ["drawing"], "label": "DXF"},
    "dwg": {"key": "dwg", "ext": ".dwg", "kinds": ["drawing"], "label": "DWG"},
    "pdf": {"key": "pdf", "ext": ".pdf", "kinds": ["drawing"], "label": "PDF"},
    "cgm": {"key": "cgm", "ext": ".cgm", "kinds": ["drawing"], "label": "CGM"},
    "svg": {"key": "svg", "ext": ".svg", "kinds": ["drawing"], "label": "SVG"},
    "hcg": {"key": "hcg", "ext": ".hcg", "kinds": ["part", "product"], "label": "HCG"},
    "wrml": {"key": "wrl", "ext": ".wrl", "kinds": ["part", "product"], "label": "VRML"},
    "obj": {"key": "obj", "ext": ".obj", "kinds": ["part"], "label": "Wavefront OBJ"},
}

# Native CATIA document extensions, used to decide open/save behaviour.
NATIVE_EXTENSIONS = {
    ".catpart": "part",
    ".catproduct": "product",
    ".catdrawing": "drawing",
    ".catprocess": "process",
    ".catshape": "part",
    ".model": "part",
    ".cgr": "part",
}

# Standard view orientations: (sight direction, up direction).
# CATIA's sight vector points *from* the camera towards the model.
VIEW_ORIENTATIONS: dict[str, dict[str, tuple[float, float, float]]] = {
    "front": {"sight": (0.0, 1.0, 0.0), "up": (0.0, 0.0, 1.0)},
    "back": {"sight": (0.0, -1.0, 0.0), "up": (0.0, 0.0, 1.0)},
    "top": {"sight": (0.0, 0.0, -1.0), "up": (0.0, 1.0, 0.0)},
    "bottom": {"sight": (0.0, 0.0, 1.0), "up": (0.0, 1.0, 0.0)},
    "left": {"sight": (1.0, 0.0, 0.0), "up": (0.0, 0.0, 1.0)},
    "right": {"sight": (-1.0, 0.0, 0.0), "up": (0.0, 0.0, 1.0)},
    "isometric": {"sight": (-1.0, 1.0, -1.0), "up": (0.0, 0.0, 1.0)},
    "iso_rear": {"sight": (1.0, -1.0, -1.0), "up": (0.0, 0.0, 1.0)},
    "dimetric": {"sight": (-1.0, 1.0, -0.5), "up": (0.0, 0.0, 1.0)},
}
