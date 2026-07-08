# -*- coding: utf-8 -*-
"""Foundation kinds for Auto Foundation.

FoundationKind is the extension point for foundation categories:

  * IsolatedFooting  — rectangular pad (Footing-Rectangular family),
                       auto-rotated to match the CAD outline
  * CustomPadFooting — triangular / L-shaped / arbitrary pad, generated
                       as a structural foundation slab from the exact
                       CAD outline
  * StripFooting, PileCap, RaftFoundation — stubs for later versions

Shape geometry (Footprint, oriented bounding box, rectangularity),
label matching and top-flush placement are shared with other
auto-modelling tools via pp_common; only Foundation-specific detection
rules (af_config) and generation (this file) are local to this bundle.
"""
import math
import os

import clr
clr.AddReference('RevitAPI')

from System.Collections.Generic import List as NetList

from Autodesk.Revit.DB import (
    FilteredElementCollector, FamilySymbol, BuiltInCategory,
    BuiltInParameter, ElementTransformUtils, Line, StorageType,
    XYZ, Floor, FloorType, CurveLoop
)
from Autodesk.Revit.DB.Structure import StructuralType

import af_config as cfg
from pp_common.geometry import Footprint, chain_lines_to_loops, norm_half_pi
from pp_common.units import get_name as _name
from pp_common import labels as pp_labels
from pp_common import placement as pp_placement


def _default_footing_label(fp):
    """Size-derived fallback name, used exactly like Auto Pile/Auto
    Column's equivalents when there is no usable label -- e.g. a CAD
    with only one layer total, so there is nowhere to pick a separate
    label layer from."""
    def mm(value_ft):
        return int(round(value_ft / cfg.MM_TO_FT))
    return u"PAD{}x{}".format(mm(fp.width_ft), mm(fp.length_ft))


def _curve_loop(points, z):
    """Builds a closed horizontal CurveLoop at elevation z from ordered
    2D points, skipping degenerate segments."""
    pts = []
    for p in points:
        if pts and abs(p[0] - pts[-1][0]) < 1e-6 and abs(p[1] - pts[-1][1]) < 1e-6:
            continue
        pts.append(p)
    if len(pts) >= 2 and (abs(pts[0][0] - pts[-1][0]) < 1e-6
                          and abs(pts[0][1] - pts[-1][1]) < 1e-6):
        pts.pop()
    if len(pts) < 3:
        raise ValueError("outline has fewer than 3 usable points")
    loop = CurveLoop()
    n = len(pts)
    for i in range(n):
        a = pts[i]
        b = pts[(i + 1) % n]
        if math.sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) < 1e-6:
            continue
        loop.Append(Line.CreateBound(XYZ(a[0], a[1], z), XYZ(b[0], b[1], z)))
    return loop


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------

class FoundationKind(object):
    """Base class for foundation categories. Subclasses implement
    generate(); footprint labelling/grouping/top-flush placement
    delegate to pp_common so other Structure-panel tools share them."""

    display_name = "foundation"

    def match_labels(self, footprints, texts):
        return pp_labels.match_labels(
            footprints, texts,
            label_max_distance_mm=cfg.LABEL_MAX_DISTANCE_MM,
            size_group_round_mm=cfg.SIZE_GROUP_ROUND_MM,
            unmatched_label_format=cfg.UNMATCHED_LABEL_FORMAT)

    def group_footprints(self, footprints):
        return pp_labels.group_shapes(footprints)

    def group_by_size_only(self, footprints):
        """No-label mode: skip CAD text entirely and group purely by
        each footprint's own derived name (mirrors Auto Pile/Auto
        Column's equivalent) -- for a CAD with only one layer total, or
        no readable label text at all. Returns OrderedDict label ->
        [footprint]."""
        for fp in footprints:
            fp.label = _default_footing_label(fp)
        return pp_labels.group_shapes(footprints)

    def generate(self, doc, level, groups):
        """Creates types + instances. Must be called inside an already
        open Transaction. groups: [{'label', 'width_ft', 'length_ft',
        'thickness_ft', 'footprints'}]. Returns (created_count,
        warnings)."""
        raise NotImplementedError(
            "{} generation is not implemented yet.".format(self.display_name))

    def _flush_top_with_level(self, doc, instances, elev, offset_bip):
        return pp_placement.flush_top_with_level(
            doc, instances, elev, offset_bip, cfg.TOP_FLUSH_TOLERANCE_FT)


class IsolatedFooting(FoundationKind):
    """Rectangular isolated pad footing, auto-rotated to the CAD outline."""

    display_name = "isolated pad footing"

    # -- family / parameter resolution -----------------------------------

    def find_base_symbol(self, doc):
        collector = (FilteredElementCollector(doc)
                     .OfCategory(BuiltInCategory.OST_StructuralFoundation)
                     .OfClass(FamilySymbol))
        wanted = [c.lower() for c in cfg.FAMILY_NAME_CANDIDATES]
        for sym in collector:
            try:
                if sym.Family.Name.lower() in wanted:
                    return sym
            except Exception:
                continue
        return None

    def load_family(self, doc):
        """Loads the default rectangular footing family from the library
        roots. Must be called inside a transaction. Returns the loaded
        file path or None."""
        for root in cfg.FAMILY_LIBRARY_ROOTS:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for cand in cfg.FAMILY_FILE_CANDIDATES:
                    if cand in filenames:
                        path = os.path.join(dirpath, cand)
                        try:
                            if doc.LoadFamily(path):
                                return path
                        except Exception:
                            pass
        return None

    def resolve_params(self, symbol):
        """Finds the actual Width / Length / Thickness type parameter
        names on this symbol (they vary between family versions).
        Returns {'width': name-or-None, 'length': ..., 'thickness': ...}."""
        found = {'width': None, 'length': None, 'thickness': None}
        candidates = (
            ('width', [c.lower() for c in cfg.WIDTH_PARAM_CANDIDATES]),
            ('length', [c.lower() for c in cfg.LENGTH_PARAM_CANDIDATES]),
            ('thickness', [c.lower() for c in cfg.THICKNESS_PARAM_CANDIDATES]),
        )
        for p in symbol.Parameters:
            try:
                if p.StorageType != StorageType.Double or p.IsReadOnly:
                    continue
                name = p.Definition.Name.strip()
            except Exception:
                continue
            low = name.lower()
            for key, cands in candidates:
                if found[key] is None and low in cands:
                    found[key] = name
        return found

    # -- generation -------------------------------------------------------

    def generate(self, doc, level, groups):
        base = self.find_base_symbol(doc)
        if base is None:
            if self.load_family(doc):
                doc.Regenerate()
                base = self.find_base_symbol(doc)
        if base is None:
            raise ValueError(
                "No rectangular footing family is loaded and none was found "
                "in the Revit library. Load '{}' into the project and try "
                "again.".format(cfg.FAMILY_NAME_CANDIDATES[0]))

        params = self.resolve_params(base)
        missing = [k for k in ('width', 'length', 'thickness')
                   if params.get(k) is None]
        if missing:
            raise ValueError(
                "Family '{}' has no writable {} type parameter(s). Adjust "
                "the candidates in af_config.py.".format(
                    base.Family.Name, ", ".join(missing)))

        family = base.Family
        existing = {}
        for sid in family.GetFamilySymbolIds():
            sym = doc.GetElement(sid)
            existing[_name(sym)] = sym

        created = 0
        warnings = []
        elev = level.ProjectElevation
        # Which model axis the family's Length parameter runs along is NOT
        # assumed (it differs between family versions): it is measured from
        # the first placed non-square instance's bounding box before any
        # rotation, and every rotation is taken relative to that axis.
        length_axis = None

        for g in groups:
            type_name = cfg.TYPE_NAME_FORMAT.format(label=g['label'])
            try:
                sym = existing.get(type_name)
                if sym is None:
                    sym = base.Duplicate(type_name)
                    existing[type_name] = sym
                sym.LookupParameter(params['width']).Set(g['width_ft'])
                sym.LookupParameter(params['length']).Set(g['length_ft'])
                sym.LookupParameter(params['thickness']).Set(g['thickness_ft'])
                if not sym.IsActive:
                    sym.Activate()
                doc.Regenerate()
            except Exception as ex:
                warnings.append(u"Type '{}': {}".format(type_name, ex))
                continue

            instances = []
            for fp in g['footprints']:
                try:
                    pt = XYZ(fp.center[0], fp.center[1], elev)
                    inst = doc.Create.NewFamilyInstance(
                        pt, sym, level, StructuralType.Footing)
                    if length_axis is None:
                        length_axis = pp_placement.detect_length_axis(
                            doc, inst, g['width_ft'], g['length_ft'])
                    delta = norm_half_pi(fp.rotation - (length_axis or 0.0))
                    if abs(delta) > 1e-6:
                        axis = Line.CreateBound(pt, XYZ(pt.X, pt.Y, pt.Z + 1.0))
                        ElementTransformUtils.RotateElement(
                            doc, inst.Id, axis, delta)
                    instances.append(inst)
                    created += 1
                except Exception as ex:
                    warnings.append(u"'{}' at ({:.2f}, {:.2f}): {}".format(
                        type_name, fp.center[0], fp.center[1], ex))

            warnings.extend(self._flush_top_with_level(
                doc, instances, elev,
                BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM))

        return created, warnings


class CustomPadFooting(FoundationKind):
    """Non-rectangular pad (triangle, L-shape, any closed outline).

    Generated as a structural foundation slab (a Floor whose type has
    IsFoundationSlab set) built from the exact CAD outline, one type per
    label with the user's thickness."""

    display_name = "custom pad footing"

    def find_base_type(self, doc):
        wanted = [c.lower() for c in cfg.FOUNDATION_SLAB_TYPE_CANDIDATES]
        fallback = None
        for ft in FilteredElementCollector(doc).OfClass(FloorType):
            try:
                if not ft.IsFoundationSlab:
                    continue
            except Exception:
                continue
            nm = _name(ft).lower()
            for w in wanted:
                if w in nm:
                    return ft
            if fallback is None:
                fallback = ft
        return fallback

    def _set_thickness(self, floor_type, thickness_ft):
        cs = floor_type.GetCompoundStructure()
        if cs is None:
            raise ValueError("type has no compound structure")
        idx = cs.StructuralMaterialIndex
        if idx < 0 or idx >= cs.LayerCount:
            widths = [cs.GetLayerWidth(i) for i in range(cs.LayerCount)]
            idx = widths.index(max(widths))
        cs.SetLayerWidth(idx, thickness_ft)
        floor_type.SetCompoundStructure(cs)

    def generate(self, doc, level, groups):
        base = self.find_base_type(doc)
        if base is None:
            raise ValueError(
                "No structural foundation slab type was found in this "
                "project. Create one (a slab type with the 'Foundation "
                "Slab' function, e.g. duplicate a floor type in Structure "
                "> Foundation > Slab) and generate again.")

        existing = {}
        for ft in FilteredElementCollector(doc).OfClass(FloorType):
            try:
                if ft.IsFoundationSlab:
                    existing[_name(ft)] = ft
            except Exception:
                continue

        created = 0
        warnings = []
        elev = level.ProjectElevation

        for g in groups:
            type_name = cfg.TYPE_NAME_FORMAT.format(label=g['label'])
            try:
                ft = existing.get(type_name)
                if ft is None:
                    ft = base.Duplicate(type_name)
                    existing[type_name] = ft
                self._set_thickness(ft, g['thickness_ft'])
            except Exception as ex:
                warnings.append(u"Type '{}': {}".format(type_name, ex))
                continue

            floors = []
            for fp in g['footprints']:
                try:
                    loops = NetList[CurveLoop]()
                    loops.Add(_curve_loop(fp.points, elev))
                    fl = Floor.Create(doc, loops, ft.Id, level.Id)
                    floors.append(fl)
                    created += 1
                except Exception as ex:
                    warnings.append(u"'{}' at ({:.2f}, {:.2f}): {}".format(
                        type_name, fp.centroid[0], fp.centroid[1], ex))

            warnings.extend(self._flush_top_with_level(
                doc, floors, elev,
                BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM))

        return created, warnings


class StripFooting(FoundationKind):
    """Continuous (strip) footing under walls — NOT implemented yet.

    Planned: trace centerline pairs / polylines on the perimeter layer,
    derive width from the offset distance and sweep a wall foundation or
    structural framing along the path."""
    display_name = "strip footing"


class PileCap(FoundationKind):
    """Pile cap with pile layout — NOT implemented yet.

    Planned: read cap outline + pile circles on a pile layer, place a
    pile-cap family with the matching pile count/arrangement."""
    display_name = "pile cap"


class RaftFoundation(FoundationKind):
    """Raft / mat slab — NOT implemented yet.

    Planned: build a floor-based foundation slab from the outline with
    openings, using SlabFoundation / floor creation APIs."""
    display_name = "raft foundation"
