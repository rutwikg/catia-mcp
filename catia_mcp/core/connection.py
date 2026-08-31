"""Connection, version detection and capability probing.

The connection object is the only thing that touches ``win32com`` directly, and
every method on it is expected to be executed on the COM apartment thread. Tool
modules go through :meth:`CatiaSession.call`, which enforces that.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from catia_mcp.core import comutil, errors
from catia_mcp.core.apartment import APARTMENT, ComApartment

logger = logging.getLogger("catia_mcp.connection")

T = TypeVar("T")

PROGIDS = ("CATIA.Application",)

# Documents.Add() keys, mapped from the document object we can reach on a doc.
_KIND_PROBES = (
    ("part", "Part"),
    ("product", "Product"),
    ("drawing", "Sheets"),
    ("process", "Process"),
)


@dataclass
class VersionInfo:
    """What we managed to learn about the CATIA on the other end."""

    family: str = "unknown"  # "V5", "V6" or "unknown"
    version: int | None = None  # 5
    release: int | None = None  # 21, 30, ...
    service_pack: int | None = None
    build: str = ""
    caption: str = ""
    install_path: str = ""
    marketing_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "version": self.version,
            "release": self.release,
            "service_pack": self.service_pack,
            "build": self.build,
            "caption": self.caption,
            "install_path": self.install_path,
            "marketing_name": self.marketing_name or self.describe(),
        }

    def describe(self) -> str:
        if self.version == 5 and self.release:
            # V5R20 and earlier used "V5RnnnSPn"; R21+ was marketed as V5-6Rxxxx.
            if self.release >= 21:
                return "CATIA V5-6R%d (R%d)" % (2010 + self.release - 10, self.release)
            return "CATIA V5R%d" % self.release
        if self.family != "unknown":
            return "CATIA %s" % self.family
        return "CATIA (release unknown)"

    def at_least(self, release: int) -> bool:
        return self.release is not None and self.release >= release


@dataclass
class SessionState:
    """Cross-call scratch state, so the model does not have to repeat itself."""

    last_sketch_name: str = ""
    last_feature_name: str = ""
    last_geoset_name: str = ""
    last_document_path: str = ""
    created: list[str] = field(default_factory=list)

    def note_feature(self, name: str) -> None:
        if name:
            self.last_feature_name = name
            self.created.append(name)
            del self.created[:-50]


class CatiaSession:
    """Owns the CATIA COM object and everything derived from it."""

    def __init__(self, apartment: ComApartment | None = None) -> None:
        self.apartment = apartment or APARTMENT
        self.app: Any = None
        self.version = VersionInfo()
        self.state = SessionState()
        self._capabilities: dict[str, Any] | None = None
        self._capture_format: tuple[str, int] | None = None
        self._spa: Any = None
        # Whether display refresh and file alerts are currently suspended.
        self.batch_mode = False

    # ── apartment plumbing ───────────────────────────────────────────────────

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run ``fn`` on the COM apartment thread."""
        return self.apartment.call(fn, *args, **kwargs)

    # ── connect / disconnect ─────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        if self.app is None:
            return False
        try:
            return self.apartment.call(self._ping, retry=False)
        except Exception:
            self._drop()
            return False

    def _ping(self) -> bool:
        try:
            _ = self.app.Name
            return True
        except Exception:
            try:
                _ = self.app.Caption
                return True
            except Exception:
                return False

    def _drop(self) -> None:
        self.app = None
        self._spa = None
        self._capabilities = None
        self._capture_format = None
        self.version = VersionInfo()

    def connect(self, *, launch: bool = False, visible: bool = True) -> dict[str, Any]:
        """Attach to a running CATIA, optionally starting one.

        Launching is opt-in on purpose. A cold CATIA start takes minutes, holds
        a licence token, and is almost never what someone wants from an
        accidental tool call.
        """
        if not comutil.HAS_PYWIN32:
            raise errors.PlatformError(
                "pywin32 is not importable in this interpreter (%s). Install it with "
                "'pip install pywin32'." % (comutil.PYWIN32_IMPORT_ERROR or "unknown error")
            )
        if sys.platform != "win32":
            raise errors.PlatformError(
                "CATIA automation is a Windows COM interface; this process is running on %s."
                % sys.platform
            )

        self.apartment.start()
        return self.apartment.call(self._connect_impl, launch, visible)

    def _connect_impl(self, launch: bool, visible: bool) -> dict[str, Any]:
        if self.app is not None and self._ping():
            return {
                "already_connected": True,
                "catia": self.version.as_dict(),
            }

        self._drop()
        attach_errors: list[str] = []

        for progid in PROGIDS:
            try:
                self.app = comutil.win32com.client.GetActiveObject(progid)
                logger.info("Attached to running %s", progid)
                break
            except Exception as exc:
                attach_errors.append("%s: %s" % (progid, errors.com_message(exc)))

        launched = False
        if self.app is None:
            if not launch:
                raise errors.NotConnectedError(
                    "No running CATIA session was found in the COM running-object table.",
                    details={"attempts": attach_errors},
                    remediation=(
                        "Start CATIA and wait for its window to appear, then retry. "
                        "To have this server start it for you, call catia_connect with "
                        "launch=true (expect a slow first start and a licence checkout). "
                        "If CATIA *is* running, check that it and this Python process run "
                        "as the same Windows user and at the same elevation - a CATIA "
                        "started as administrator is invisible to a non-elevated client."
                    ),
                )
            for progid in PROGIDS:
                try:
                    self.app = comutil.win32com.client.Dispatch(progid)
                    launched = True
                    logger.info("Launched %s", progid)
                    break
                except Exception as exc:
                    attach_errors.append("launch %s: %s" % (progid, errors.com_message(exc)))

        if self.app is None:
            raise errors.NotConnectedError(
                "Could not reach CATIA over COM.",
                details={"attempts": attach_errors},
            )

        if launched and visible:
            try:
                self.app.Visible = True
            except Exception:
                pass

        self.version = self._detect_version()
        self._capabilities = None
        return {
            "already_connected": False,
            "launched": launched,
            "catia": self.version.as_dict(),
        }

    def disconnect(self) -> dict[str, Any]:
        """Release the COM reference. CATIA itself keeps running."""
        was = self.app is not None
        self.apartment.call(self._drop)
        return {"was_connected": was}

    def require(self) -> Any:
        """Return the live CATIA application object or raise."""
        if self.app is None:
            raise errors.NotConnectedError("Not connected to CATIA.")
        return self.app

    def ensure(self) -> Any:
        """Reattach transparently if a previously good connection went stale."""
        if self.app is not None:
            try:
                if self._ping():
                    return self.app
            except Exception:
                pass
        self._drop()
        self._connect_impl(launch=False, visible=True)
        return self.app

    # ── version ──────────────────────────────────────────────────────────────

    def _detect_version(self) -> VersionInfo:
        info = VersionInfo()
        app = self.app

        config = comutil.safe(app, "SystemConfiguration")
        if config is not None:
            version = comutil.safe(config, "Version")
            release = comutil.safe(config, "Release")
            sp = comutil.safe(config, "ServicePack")
            try:
                info.version = int(version) if version is not None else None
            except Exception:
                pass
            try:
                info.release = int(release) if release is not None else None
            except Exception:
                pass
            try:
                info.service_pack = int(sp) if sp is not None else None
            except Exception:
                pass

        info.caption = str(comutil.safe(app, "Caption", "") or "")

        if info.version == 5:
            info.family = "V5"
        elif info.version == 6:
            info.family = "V6"
        else:
            text = "%s %s" % (info.caption, comutil.safe(app, "Name", "") or "")
            if re.search(r"(?i)\bV5\b|V5-6R", text):
                info.family = "V5"
                info.version = info.version or 5
            elif re.search(r"(?i)\bV6\b|3DEXPERIENCE", text):
                info.family = "V6"
                info.version = info.version or 6

        service = comutil.safe(app, "SystemService")
        if service is not None:
            for key in ("CATInstallPath", "CATIA_INSTALL_PATH", "CATDLLPath"):
                value = comutil.safe_call(service, "Environ", key, default="")
                if value:
                    info.install_path = str(value)
                    break
            build = comutil.safe_call(service, "Environ", "CATVersion", default="")
            if build:
                info.build = str(build)

        info.marketing_name = info.describe()
        logger.info("Connected to %s", info.marketing_name)
        return info

    # ── capabilities ─────────────────────────────────────────────────────────

    def capabilities(self, *, refresh: bool = False) -> dict[str, Any]:
        """Probe what this installation can actually do.

        Everything here is discovered by attempting the real call, because a
        CATIA licence bundle - not the release number - decides whether GSD or
        Sheet Metal are usable.
        """
        if self._capabilities is not None and not refresh:
            return self._capabilities
        self._capabilities = self.apartment.call(self._probe_capabilities)
        return self._capabilities

    def _probe_capabilities(self) -> dict[str, Any]:
        app = self.require()
        caps: dict[str, Any] = {}

        caps["script_bridge"] = comutil.has_member(
            comutil.safe(app, "SystemService"), "Evaluate"
        )
        caps["measure"] = comutil.safe_call(app, "GetWorkbench", "SPAWorkbench") is not None
        caps["file_alerts_control"] = comutil.has_member(app, "DisplayFileAlerts")
        caps["refresh_control"] = comutil.has_member(app, "RefreshDisplay")
        caps["start_command"] = comutil.has_member(app, "StartCommand")

        doc = comutil.safe(app, "ActiveDocument")
        part = comutil.safe(doc, "Part") if doc is not None else None
        product = comutil.safe(doc, "Product") if doc is not None else None

        if part is not None:
            caps["part_design"] = comutil.safe(part, "ShapeFactory") is not None
            caps["gsd"] = comutil.safe(part, "HybridShapeFactory") is not None
            caps["knowledge"] = comutil.safe(part, "Relations") is not None
            caps["sheet_metal"] = (
                comutil.safe_call(part, "GetCustomerFactory", "SheetMetalFactory") is not None
            )
        else:
            caps["part_design"] = None
            caps["gsd"] = None
            caps["knowledge"] = None
            caps["sheet_metal"] = None

        if product is not None:
            caps["assembly"] = comutil.safe(product, "Products") is not None
            caps["kinematics"] = (
                comutil.safe_call(product, "GetTechnologicalObject", "Mechanisms") is not None
            )
        else:
            caps["assembly"] = None
            caps["kinematics"] = None

        caps["drafting"] = comutil.safe(doc, "Sheets") is not None if doc is not None else None
        caps["notes"] = (
            "Entries reported as null need an open document of the matching type before "
            "they can be probed. Open or create one and call catia_capabilities again."
        )
        return caps

    # ── document access ──────────────────────────────────────────────────────

    @property
    def documents(self) -> Any:
        return self.require().Documents

    def active_document(self) -> Any:
        app = self.require()
        try:
            doc = app.ActiveDocument
        except Exception as exc:
            raise errors.NoActiveDocumentError(
                "CATIA has no active document (%s)." % errors.com_message(exc)
            ) from exc
        if doc is None:
            raise errors.NoActiveDocumentError("CATIA has no active document.")
        return doc

    def document_kind(self, doc: Any) -> str:
        for kind, member in _KIND_PROBES:
            if comutil.safe(doc, member) is not None:
                return kind
        return "unknown"

    def active_part(self) -> Any:
        doc = self.active_document()
        part = comutil.safe(doc, "Part")
        if part is None:
            raise errors.WrongDocumentTypeError(
                "The active document '%s' is a %s document, but this tool needs a Part."
                % (comutil.name_of(doc, "?"), self.document_kind(doc))
            )
        return part

    def active_product(self) -> Any:
        doc = self.active_document()
        product = comutil.safe(doc, "Product")
        # A PartDocument also exposes a .Product - that is where CATIA keeps the
        # part number and revision - so the kind has to be checked explicitly or
        # assembly tools would silently operate on a part.
        if product is None or self.document_kind(doc) != "product":
            raise errors.WrongDocumentTypeError(
                "The active document '%s' is a %s document, but this tool needs a Product "
                "(assembly)." % (comutil.name_of(doc, "?"), self.document_kind(doc))
            )
        return product

    def active_drawing(self) -> Any:
        doc = self.active_document()
        sheets = comutil.safe(doc, "Sheets")
        if sheets is None:
            raise errors.WrongDocumentTypeError(
                "The active document '%s' is a %s document, but this tool needs a Drawing."
                % (comutil.name_of(doc, "?"), self.document_kind(doc))
            )
        return doc

    def selection(self) -> Any:
        return self.active_document().Selection

    def shape_factory(self) -> Any:
        part = self.active_part()
        factory = comutil.safe(part, "ShapeFactory")
        if factory is None:
            raise errors.UnsupportedCapabilityError(
                "Part Design (ShapeFactory) is not available in this CATIA session."
            )
        return factory

    def hybrid_factory(self) -> Any:
        part = self.active_part()
        factory = comutil.safe(part, "HybridShapeFactory")
        if factory is None:
            raise errors.UnsupportedCapabilityError(
                "Generative Shape Design (HybridShapeFactory) is not licensed or not "
                "available in this CATIA session."
            )
        return factory

    def measurable_workbench(self) -> Any:
        app = self.require()
        if self._spa is None:
            self._spa = comutil.safe_call(app, "GetWorkbench", "SPAWorkbench")
        if self._spa is None:
            raise errors.UnsupportedCapabilityError(
                "The SPAWorkbench (measurement) is not available in this CATIA session."
            )
        return self._spa

    def measurable(self, reference: Any) -> Any:
        try:
            return self.measurable_workbench().GetMeasurable(reference)
        except Exception as exc:
            raise errors.translate(exc) from exc

    # ── housekeeping ─────────────────────────────────────────────────────────

    def update_part(self, part: Any = None) -> None:
        target = part if part is not None else self.active_part()
        try:
            target.Update()
        except Exception as exc:
            raise errors.OperationFailedError(
                "CATIA could not update the part: %s" % errors.com_message(exc),
                remediation=(
                    "An earlier feature is probably in error. Call catia_list_features "
                    "with include_errors=true to find it."
                ),
            ) from exc

    def refresh_view(self) -> None:
        app = self.app
        if app is None:
            return
        try:
            app.ActiveWindow.ActiveViewer.Reframe()
        except Exception:
            pass

    def set_visual_batching(self, *, quiet: bool) -> None:
        """Turn display refresh and file alerts off during bulk work."""
        self.batch_mode = quiet
        app = self.app
        if app is None:
            return
        for member, value in (("RefreshDisplay", not quiet), ("DisplayFileAlerts", not quiet)):
            try:
                setattr(app, member, value)
            except Exception:
                pass

    def environment_report(self) -> dict[str, Any]:
        """Everything useful for diagnosing a failed connection, without CATIA."""
        import platform
        import struct

        report: dict[str, Any] = {
            "platform": sys.platform,
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "python_bits": struct.calcsize("P") * 8,
            "pywin32_available": comutil.HAS_PYWIN32,
            "pywin32_error": comutil.PYWIN32_IMPORT_ERROR,
            "apartment_thread_running": self.apartment.alive,
            "constants_override": os.environ.get("CATIA_MCP_CONSTANTS") or None,
            "connected": self.app is not None,
        }
        if comutil.HAS_PYWIN32:
            try:
                import win32api  # noqa: PLC0415

                report["pywin32_build"] = getattr(win32api, "__file__", "")
            except Exception:
                pass
            report["catia_progid_registered"] = _progid_registered("CATIA.Application")
        if self.app is not None:
            report["catia"] = self.version.as_dict()
        return report


def _progid_registered(progid: str) -> bool:
    try:
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid):
            return True
    except Exception:
        return False


SESSION = CatiaSession()
