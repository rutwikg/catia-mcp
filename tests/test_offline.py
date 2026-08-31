"""Tests that run without CATIA, without Windows and without COM.

Everything here exercises the logic that sits between the model and CATIA:
error classification, the reference-token grammar, adaptive invocation, the
result envelope, matrix maths and capture-format sniffing. The parts that must
talk to CATIA are covered by ``tests/smoke_live.py``, which needs a running
CATIA and is not part of this suite.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catia_mcp.core import comutil, constants, errors, result  # noqa: E402
from catia_mcp.core.apartment import ComApartment  # noqa: E402

# ── fakes ────────────────────────────────────────────────────────────────────

class FakeComError(Exception):
    """Shaped like pywintypes.com_error: (hresult, text, excepinfo, argerror)."""

    def __init__(self, hresult: int, text: str = "", description: str = ""):
        excepinfo = (0, "CATIA", description, "", 0, hresult)
        super().__init__(hresult, text, excepinfo, None)


class FakeCollection:
    """A CATIA-style 1-based collection."""

    def __init__(self, items):
        self._items = list(items)

    @property
    def Count(self):
        return len(self._items)

    def Item(self, key):
        if isinstance(key, int):
            if key < 1 or key > len(self._items):
                raise FakeComError(-2147352567, "index out of range")
            return self._items[key - 1]
        for item in self._items:
            if getattr(item, "Name", None) == key:
                return item
        raise FakeComError(-2147352567, "no such item")


class FakeNamed:
    def __init__(self, name):
        self.Name = name


# ── error taxonomy ───────────────────────────────────────────────────────────

def test_busy_hresult_is_retryable_and_translated():
    exc = FakeComError(errors.RPC_E_CALL_REJECTED, "Call was rejected by callee.")
    assert errors.is_retryable(exc)
    assert not errors.is_dead(exc)
    translated = errors.translate(exc)
    assert isinstance(translated, errors.CatiaBusyError)
    assert translated.code == "catia_busy"
    assert "dialog" in translated.remediation


def test_dead_hresult_maps_to_disconnected():
    exc = FakeComError(errors.RPC_S_SERVER_UNAVAILABLE, "The RPC server is unavailable.")
    assert errors.is_dead(exc)
    assert errors.translate(exc).code == "catia_disconnected"


def test_missing_member_maps_to_unsupported_capability():
    exc = FakeComError(errors.DISP_E_MEMBERNOTFOUND, "Member not found.")
    assert errors.is_missing_member(exc)
    assert errors.translate(exc).code == "unsupported_capability"


def test_unknown_failure_keeps_the_hresult_for_diagnosis():
    exc = FakeComError(-2147467259, "Unspecified error", "The radius is too large")
    translated = errors.translate(exc)
    assert translated.code == "operation_failed"
    assert translated.details["hresult"] == "0x80004005"
    assert "radius is too large" in translated.message


def test_com_message_prefers_the_catia_description():
    exc = FakeComError(-2147467259, "generic", "Profile is not closed")
    assert "Profile is not closed" in errors.com_message(exc)


# ── result envelope ──────────────────────────────────────────────────────────

def test_tool_wrapper_converts_catia_errors_into_data():
    @result.tool
    def boom():
        raise errors.ElementNotFoundError("no such face")

    payload = boom()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "element_not_found"
    assert payload["error"]["remediation"]


def test_tool_wrapper_translates_raw_com_errors():
    @result.tool
    def boom():
        raise FakeComError(errors.RPC_E_CALL_REJECTED, "busy")

    assert boom()["error"]["code"] == "catia_busy"


def test_tool_wrapper_adds_ok_to_bare_dicts():
    @result.tool
    def fine():
        return {"created": "Pad.1"}

    payload = fine()
    assert payload["ok"] is True and payload["created"] == "Pad.1"


def test_round_xyz():
    assert result.round_xyz([1.23456789, -2.0, 0.0]) == {"x": 1.2346, "y": -2.0, "z": 0.0}


# ── adaptive invocation ──────────────────────────────────────────────────────

def test_try_variants_returns_the_first_that_works():
    calls = []

    def first():
        calls.append("first")
        raise FakeComError(errors.DISP_E_BADPARAMCOUNT, "wrong number of arguments")

    def second():
        calls.append("second")
        return "made it"

    label, value = comutil.try_variants(
        [("new-signature", first), ("old-signature", second)], what="do the thing"
    )
    assert (label, value) == ("old-signature", "made it")
    assert calls == ["first", "second"]


def test_try_variants_reraises_busy_without_trying_the_rest():
    tried = []

    def busy():
        tried.append("busy")
        raise FakeComError(errors.RPC_E_CALL_REJECTED, "busy")

    def never():  # pragma: no cover - must not run
        tried.append("never")
        return 1

    with pytest.raises(Exception) as caught:
        comutil.try_variants([("a", busy), ("b", never)], what="x")
    assert errors.is_retryable(caught.value)
    assert tried == ["busy"]


def test_try_variants_reports_every_attempt_when_all_fail():
    def fail(message):
        def call():
            raise FakeComError(-2147467259, message)

        return call

    with pytest.raises(errors.OperationFailedError) as caught:
        comutil.try_variants(
            [("a", fail("no a")), ("b", fail("no b"))], what="create a widget"
        )
    attempts = caught.value.details["attempts"]
    assert len(attempts) == 2
    assert "create a widget" in caught.value.message


# ── defensive COM access ─────────────────────────────────────────────────────

def test_com_iter_is_one_based_and_skips_broken_items():
    class Flaky(FakeCollection):
        def Item(self, key):
            if key == 2:
                raise FakeComError(-2147467259, "deleted")
            return super().Item(key)

    items = list(comutil.com_iter(Flaky([FakeNamed("a"), FakeNamed("b"), FakeNamed("c")])))
    assert [i.Name for i in items] == ["a", "c"]


def test_safe_and_has_member_tolerate_missing_attributes():
    class Sparse:
        @property
        def Broken(self):
            raise FakeComError(errors.DISP_E_MEMBERNOTFOUND, "nope")

    obj = Sparse()
    assert comutil.safe(obj, "Broken", "fallback") == "fallback"
    assert comutil.has_member(obj, "Broken") is False
    assert comutil.name_of(FakeNamed("Pad.1")) == "Pad.1"


def test_vb_literal_escapes_quotes():
    assert comutil._vb_literal('say "hi"') == '"say ""hi"""'
    assert comutil._vb_literal(True) == "True"
    assert comutil._vb_literal(2.5) == "2.5"


# ── apartment ────────────────────────────────────────────────────────────────

def test_apartment_retries_transient_rejections_then_succeeds():
    apartment = ComApartment(retry_attempts=4, retry_initial_delay=0.001)
    state = {"calls": 0}

    def flaky():
        state["calls"] += 1
        if state["calls"] < 3:
            raise FakeComError(errors.RPC_E_CALL_REJECTED, "busy")
        return "done"

    assert apartment._invoke(flaky, (), {}, True) == "done"
    assert state["calls"] == 3


def test_apartment_gives_up_with_a_busy_error():
    apartment = ComApartment(retry_attempts=2, retry_initial_delay=0.001)

    def always_busy():
        raise FakeComError(errors.RPC_E_CALL_REJECTED, "busy")

    with pytest.raises(errors.CatiaBusyError):
        apartment._invoke(always_busy, (), {}, True)


def test_apartment_does_not_retry_real_failures():
    apartment = ComApartment(retry_attempts=5, retry_initial_delay=0.001)
    state = {"calls": 0}

    def broken():
        state["calls"] += 1
        raise FakeComError(-2147467259, "profile not closed")

    with pytest.raises(FakeComError):
        apartment._invoke(broken, (), {}, True)
    assert state["calls"] == 1  # no retry: this is a real failure, not a busy signal


# ── reference grammar ────────────────────────────────────────────────────────

def test_parse_xyz_accepts_the_formats_a_model_will_produce():
    from catia_mcp.core.refs import parse_xyz

    for text in ("10,0,25", " 10 0 25 ", "(10, 0, 25)", "[10;0;25]"):
        assert parse_xyz(text) == (10.0, 0.0, 25.0)


def test_parse_xyz_rejects_the_wrong_arity():
    from catia_mcp.core.refs import parse_xyz

    with pytest.raises(errors.InvalidArgumentError):
        parse_xyz("10,0")
    with pytest.raises(errors.InvalidArgumentError):
        parse_xyz("a,b,c")


def test_topology_token_patterns():
    from catia_mcp.core import refs

    assert refs._TOPO_INDEX.match("face#3").groups() == ("face", "3")
    assert refs._TOPO_INDEX.match("EDGE # 12").groups() == ("EDGE", "12")
    assert refs._TOPO_INDEX.match("face@1,2,3") is None
    assert refs._TOPO_NEAR.match("edge@10,0,5").group(2) == "10,0,5"


def test_cross_product_is_normalised():
    from catia_mcp.core.refs import cross

    normal = cross([2.0, 0.0, 0.0], [0.0, 3.0, 0.0])
    assert normal == pytest.approx([0.0, 0.0, 1.0])
    assert cross([1.0, 0.0, 0.0], [2.0, 0.0, 0.0]) == pytest.approx([0.0, 0.0, 0.0])


def test_reference_help_documents_every_token_family():
    from catia_mcp.core.refs import GRAMMAR_HELP

    tokens = " ".join(entry["token"] for entry in GRAMMAR_HELP["tokens"])
    for expected in ("xy", "last", "face#3", "face@12,0,40", "name:Pad.1"):
        assert expected in tokens


# ── assembly maths ───────────────────────────────────────────────────────────

def test_euler_matrix_round_trips_through_decompose():
    from catia_mcp.tools.assembly import _decompose, _euler_matrix

    rotation = _euler_matrix(30.0, -20.0, 45.0)
    decomposed = _decompose(rotation + [1.0, 2.0, 3.0])
    assert decomposed["rotation_deg"]["rx"] == pytest.approx(30.0, abs=1e-4)
    assert decomposed["rotation_deg"]["ry"] == pytest.approx(-20.0, abs=1e-4)
    assert decomposed["rotation_deg"]["rz"] == pytest.approx(45.0, abs=1e-4)
    assert decomposed["translation_mm"] == {"x": 1.0, "y": 2.0, "z": 3.0}


def test_identity_euler_matrix():
    from catia_mcp.tools.assembly import _euler_matrix

    assert _euler_matrix(0, 0, 0) == pytest.approx([1, 0, 0, 0, 1, 0, 0, 0, 1])


def test_rotation_composition_is_associative_with_identity():
    from catia_mcp.tools.assembly import _compose, _euler_matrix

    identity = _euler_matrix(0, 0, 0)
    rotation = _euler_matrix(10, 20, 30)
    assert _compose(rotation, identity) == pytest.approx(rotation)
    assert _compose(identity, rotation) == pytest.approx(rotation)


def test_composing_two_z_rotations_adds_the_angles():
    from catia_mcp.tools.assembly import _compose, _decompose, _euler_matrix

    combined = _compose(_euler_matrix(0, 0, 20), _euler_matrix(0, 0, 25))
    assert _decompose(combined + [0, 0, 0])["rotation_deg"]["rz"] == pytest.approx(45.0)


# ── capture format probing ───────────────────────────────────────────────────

def test_capture_probe_learns_the_mapping_from_file_magic(tmp_path):
    from catia_mcp.core import capture

    written = {2: b"BM....", 5: b"\xff\xd8\xff....", 6: capture.constants.FILE_MAGIC["png"]}

    class FakeViewer:
        def CaptureToFile(self, code, path):
            if code not in written:
                raise FakeComError(-2147467259, "unsupported format")
            with open(path, "wb") as handle:
                handle.write(written[code])

    formats = capture.discover_formats(FakeViewer())
    assert formats == {"bmp": 2, "jpeg": 5, "png": 6}

    cache = capture.CaptureFormats()
    assert cache.get(FakeViewer()) == ("png", 6)
    assert cache.get(FakeViewer(), "jpg") == ("jpeg", 5)
    with pytest.raises(errors.UnsupportedCapabilityError):
        cache.get(FakeViewer(), "tiff")


def test_capture_probe_falls_back_when_nothing_is_recognisable():
    from catia_mcp.core import capture

    class MuteViewer:
        def CaptureToFile(self, code, path):
            raise FakeComError(-2147467259, "no")

    cache = capture.CaptureFormats()
    name, code = cache.get(MuteViewer())
    assert name in capture.PREFERRED and isinstance(code, int)


# ── constants ────────────────────────────────────────────────────────────────

def test_constants_lookup_and_unknown_name():
    assert constants.const("catTangencyFilletEdgePropagation") == 1
    with pytest.raises(KeyError):
        constants.const("catNotARealConstant")


def test_every_export_format_declares_the_document_kinds_it_suits():
    for name, spec in constants.EXPORT_FORMATS.items():
        assert spec["kinds"], name
        assert set(spec["kinds"]) <= {"part", "product", "drawing"}, name
        assert spec["ext"].startswith("."), name


def test_view_orientations_are_unit_ish_and_non_degenerate():
    for name, spec in constants.VIEW_ORIENTATIONS.items():
        sight = spec["sight"]
        up = spec["up"]
        assert math.hypot(*sight) > 0, name
        # The camera's up vector must not be parallel to its line of sight.
        cross = (
            sight[1] * up[2] - sight[2] * up[1],
            sight[2] * up[0] - sight[0] * up[2],
            sight[0] * up[1] - sight[1] * up[0],
        )
        assert math.sqrt(sum(c * c for c in cross)) > 1e-6, name


# ── registration ─────────────────────────────────────────────────────────────

def test_the_whole_tool_surface_builds_without_catia():
    from catia_mcp.server import tool_inventory

    inventory = tool_inventory()
    assert inventory["tool_count"] > 120
    names = [
        item["name"] for group in inventory["groups"].values() for item in group
    ]
    assert len(names) == len(set(names)), "tool names must be unique"
    assert all(name.startswith("catia_") for name in names)
    for expected in (
        "catia_connect",
        "catia_check_environment",
        "catia_reference_help",
        "catia_list_faces",
        "catia_list_edges",
        "catia_list_vertices",
        "catia_fillet",
        "catia_screenshot",
    ):
        assert expected in names


def test_read_only_tools_are_marked_as_such():
    from catia_mcp.server import tool_inventory

    inventory = tool_inventory()
    flags = {
        item["name"]: item["readonly"]
        for group in inventory["groups"].values()
        for item in group
    }
    assert flags["catia_list_faces"] is True
    assert flags["catia_status"] is True
    assert flags["catia_pad"] is False


def test_sketch_constraint_table_is_self_consistent():
    from catia_mcp.tools.sketch import CONSTRAINT_KINDS

    for name, spec in CONSTRAINT_KINDS.items():
        assert spec["elements"] in (1, 2, 3), name
        constants.const(spec["const"])  # raises if the constant is unknown


def test_assembly_constraint_table_is_self_consistent():
    from catia_mcp.tools.assembly import CONSTRAINT_KINDS

    for name, spec in CONSTRAINT_KINDS.items():
        assert spec["elements"] in (1, 2), name
        constants.const(spec["const"])
