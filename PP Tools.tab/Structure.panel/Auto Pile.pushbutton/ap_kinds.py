# -*- coding: utf-8 -*-
"""Pile family resolution for Auto Pile.

Mirrors Auto Foundation's af_kinds.py pattern: find or load the base
family, then CONFIRM its real parameter names at runtime rather than
assuming them, since they vary between family authors/versions.

Three kinds, matching the PileShape hierarchy in ap_shapes.py:

  * RoundPileFamily   — circular piles (M_Pile_Beton or equivalent,
                        found in Revit's library); implemented
  * SquarePileFamily  — square/rectangular piles (PP_Pile-Square-
                        Concrete, bundled with this pushbutton — no
                        concrete family of this shape exists in
                        Revit's standard library; verified by
                        searching every Structural Foundations folder);
                        implemented, including CAD-rotation matching
  * CustomPileFamily  — arbitrary outlines; stub, see its docstring
"""
import os

import clr
clr.AddReference('RevitAPI')

from Autodesk.Revit.DB import (
    FilteredElementCollector, FamilySymbol, BuiltInCategory, StorageType,
    Material, XYZ, Line, ElementTransformUtils, BuiltInParameter
)
from Autodesk.Revit.DB.Structure import StructuralType

import ap_config as cfg
from pp_common.units import get_name as _name
from pp_common.geometry import norm_half_pi
from pp_common import placement as pp_placement


class PileFamily(object):
    """Base for a pile family kind: finds/loads the base symbol and
    confirms its real type-parameter names (and, optionally, a
    Structural Material parameter — not every family has one)."""

    display_name = "pile"
    name_candidates = []
    file_candidates = []
    library_roots = []
    size_param_candidates = {}  # {'width': [...], ...} -- kind-specific

    def find_base_symbol(self, doc):
        collector = (FilteredElementCollector(doc)
                     .OfCategory(BuiltInCategory.OST_StructuralFoundation)
                     .OfClass(FamilySymbol))
        wanted = [c.lower() for c in self.name_candidates]
        for sym in collector:
            try:
                if sym.Family.Name.lower() in wanted:
                    return sym
            except Exception:
                continue
        return None

    def load_family(self, doc, bundle_dir=None):
        """Loads the base family. bundle_dir (this pushbutton's own
        folder) is checked first for bundled families (e.g. the square
        pile), then self.library_roots for ones expected to already be
        part of the Revit install. Must be called inside a transaction.
        Returns the loaded file path or None."""
        if bundle_dir:
            for cand in self.file_candidates:
                path = os.path.join(bundle_dir, cand)
                if os.path.isfile(path):
                    try:
                        if doc.LoadFamily(path):
                            return path
                    except Exception:
                        pass
        for root in self.library_roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for cand in self.file_candidates:
                    if cand in filenames:
                        path = os.path.join(dirpath, cand)
                        try:
                            if doc.LoadFamily(path):
                                return path
                        except Exception:
                            pass
        return None

    def resolve_params(self, symbol):
        """Finds the actual size/depth (and, if present, material) type
        parameter names on this symbol. Returns a dict with 'depth' and
        this kind's size keys always present (None if not found), plus
        'material' (None when the family has no such parameter at all —
        this is normal, not an error; e.g. M_Pile_Beton has none).

        Kind-specific size keys are matched before the generic 'depth'
        fallback list, and a physical parameter that has already been
        claimed for one logical role is never claimed for another —
        DEPTH_PARAM_CANDIDATES includes "Length" (some pile families
        genuinely call their depth parameter that), which would
        otherwise collide with a square pile's own cross-section
        "Length" parameter and silently steal it.

        'embedment' is optional (None when the family has no such
        parameter — most won't) and, when present, is zeroed out in
        generate() so Depth alone is the pile's exact total length; see
        EMBEDMENT_PARAM_CANDIDATES in ap_config.py for why."""
        found = {key: None for key in self.size_param_candidates}
        found['depth'] = None
        found['embedment'] = None
        found['material'] = None

        ordered_keys = (list(self.size_param_candidates.keys())
                       + ['depth', 'embedment'])
        candidates = dict(self.size_param_candidates)
        candidates['depth'] = cfg.DEPTH_PARAM_CANDIDATES
        candidates['embedment'] = cfg.EMBEDMENT_PARAM_CANDIDATES
        candidates = {k: [c.lower() for c in v] for k, v in candidates.items()}

        claimed_names = set()
        for p in symbol.Parameters:
            try:
                name = p.Definition.Name.strip()
            except Exception:
                continue
            if name in claimed_names:
                continue
            low = name.lower()
            if p.StorageType == StorageType.Double and not p.IsReadOnly:
                for key in ordered_keys:
                    if found.get(key) is None and low in candidates[key]:
                        found[key] = name
                        claimed_names.add(name)
                        break
            elif p.StorageType == StorageType.ElementId and not p.IsReadOnly:
                if found['material'] is None and low in [
                        c.lower() for c in cfg.MATERIAL_PARAM_CANDIDATES]:
                    found['material'] = name
                    claimed_names.add(name)
        return found

    def find_or_create_concrete_material(self, doc):
        """Finds a material matching CONCRETE_MATERIAL_NAME_CANDIDATES.
        Does not create one: every tested Revit project/template ships
        at least one concrete material (verified: the Structural
        Foundation family template alone provides "Concrete - Cast-in-
        Place Concrete"), so creation has not been needed in practice.
        Returns the Material element, or None if truly nothing matches."""
        wanted = [c.lower() for c in cfg.CONCRETE_MATERIAL_NAME_CANDIDATES]
        fallback = None
        for m in FilteredElementCollector(doc).OfClass(Material):
            low = _name(m).lower()
            if low in wanted:
                return m
            if fallback is None and "concrete" in low:
                fallback = m
        return fallback

    # -- generation -------------------------------------------------------

    def _apply_size(self, sym, params, group):
        """Subclass hook: sets this kind's size parameter(s) on the
        duplicated type from the group's resolved values."""
        raise NotImplementedError

    def _needs_rotation(self):
        """True for shapes with an orientation (square/rectangular);
        round piles are rotationally symmetric so never need it."""
        return False

    def generate(self, doc, level, groups, bundle_dir=None):
        """Creates types + instances. Must be called inside an already
        open Transaction. groups: [{'label', 'depth_ft', 'shapes', ...
        kind-specific size keys}]. Returns (created_count, warnings)."""
        base = self.find_base_symbol(doc)
        if base is None:
            if self.load_family(doc, bundle_dir=bundle_dir):
                doc.Regenerate()
                base = self.find_base_symbol(doc)
        if base is None:
            raise ValueError(
                "No {} family ('{}') is loaded and none could be found {}. "
                "Load it into the project manually and try again.".format(
                    self.display_name, self.name_candidates[0],
                    "in the Revit library" if self.library_roots
                    else "bundled with this pushbutton"))

        params = self.resolve_params(base)
        missing = [k for k in self.size_param_candidates if not params.get(k)]
        if not params.get('depth'):
            missing.append('depth')
        if missing:
            raise ValueError(
                "Family '{}' has no writable {} type parameter(s). Adjust "
                "the candidates in ap_config.py.".format(
                    base.Family.Name, ", ".join(missing)))

        material = None
        if params.get('material'):
            material = self.find_or_create_concrete_material(doc)

        family = base.Family
        existing = {}
        for sid in family.GetFamilySymbolIds():
            sym = doc.GetElement(sid)
            existing[_name(sym)] = sym

        created = 0
        warnings = []
        elev = level.ProjectElevation
        # Which model axis the family's Length (or long-side) parameter
        # runs along is NOT assumed (see pp_common.placement.
        # detect_length_axis) -- measured once from the first placed
        # non-square instance, every rotation taken relative to it.
        length_axis = None

        for g in groups:
            type_name = cfg.TYPE_NAME_FORMAT.format(label=g['label'])
            try:
                sym = existing.get(type_name)
                if sym is None:
                    sym = base.Duplicate(type_name)
                    existing[type_name] = sym
                self._apply_size(sym, params, g)
                sym.LookupParameter(params['depth']).Set(g['depth_ft'])
                if params.get('embedment'):
                    # zeroed so Depth alone is the pile's exact total
                    # length -- some families (M_Pile_Beton confirmed) add
                    # this on top of Depth otherwise (see ap_config.py)
                    sym.LookupParameter(params['embedment']).Set(0.0)
                if material is not None and params.get('material'):
                    sym.LookupParameter(params['material']).Set(material.Id)
                if not sym.IsActive:
                    sym.Activate()
                doc.Regenerate()
            except Exception as ex:
                warnings.append(u"Type '{}': {}".format(type_name, ex))
                continue

            instances = []
            for shape in g['shapes']:
                try:
                    pt = XYZ(shape.center[0], shape.center[1], elev)
                    inst = doc.Create.NewFamilyInstance(
                        pt, sym, level, StructuralType.Footing)
                    if self._needs_rotation():
                        if length_axis is None:
                            length_axis = pp_placement.detect_length_axis(
                                doc, inst, g['width_ft'], g['length_ft'])
                        rotation = getattr(shape, 'rotation', 0.0)
                        delta = norm_half_pi(rotation - (length_axis or 0.0))
                        if abs(delta) > 1e-6:
                            axis = Line.CreateBound(pt, XYZ(pt.X, pt.Y, pt.Z + 1.0))
                            ElementTransformUtils.RotateElement(
                                doc, inst.Id, axis, delta)
                    instances.append(inst)
                    created += 1
                except Exception as ex:
                    warnings.append(u"'{}' at ({:.2f}, {:.2f}): {}".format(
                        type_name, shape.center[0], shape.center[1], ex))

            warnings.extend(pp_placement.flush_top_with_level(
                doc, instances, elev,
                BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM))

        return created, warnings


class RoundPileFamily(PileFamily):
    display_name = "round pile"
    name_candidates = cfg.ROUND_FAMILY_NAME_CANDIDATES
    file_candidates = cfg.ROUND_FAMILY_FILE_CANDIDATES
    library_roots = cfg.ROUND_FAMILY_LIBRARY_ROOTS
    size_param_candidates = {'diameter': cfg.DIAMETER_PARAM_CANDIDATES}

    def _apply_size(self, sym, params, group):
        sym.LookupParameter(params['diameter']).Set(group['diameter_ft'])


class SquarePileFamily(PileFamily):
    display_name = "square pile"
    name_candidates = cfg.SQUARE_FAMILY_NAME_CANDIDATES
    file_candidates = cfg.SQUARE_FAMILY_FILE_CANDIDATES
    library_roots = []  # bundle-local only; see load_family
    size_param_candidates = {
        'width': cfg.WIDTH_PARAM_CANDIDATES,
        'length': cfg.LENGTH_PARAM_CANDIDATES,
    }

    def _apply_size(self, sym, params, group):
        sym.LookupParameter(params['width']).Set(group['width_ft'])
        sym.LookupParameter(params['length']).Set(group['length_ft'])

    def _needs_rotation(self):
        return True


class CustomPileFamily(PileFamily):
    """Arbitrary (non-circle, non-rectangular) pile outline — NOT
    implemented yet. Matches ap_shapes.CustomPileShape and af_kinds.py's
    StripFooting/PileCap/RaftFoundation stubs: script.py's on_generate
    already routes "Custom" rows around both real kinds and reports them
    as skipped rather than calling this, so generate() here only needs
    to fail clearly if something ever calls it directly.

    Planned: mirror Auto Foundation's CustomPadFooting treatment — build
    a structural foundation element from the exact CAD outline (a Floor-
    based foundation slab, or a dedicated custom pile/pier family once
    one is confirmed) instead of a duplicated symbol's rectangular/
    circular profile."""

    display_name = "custom pile"

    def generate(self, doc, level, groups):
        raise NotImplementedError(
            "Custom-shape pile generation is not implemented yet.")
