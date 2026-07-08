# -*- coding: utf-8 -*-
"""Concrete column family resolution for Auto Column.

Mirrors af_kinds.py / ap_kinds.py's pattern: find or load the base
family, then CONFIRM its real parameter names at runtime rather than
assuming them. Category is OST_StructuralColumns (not
OST_StructuralFoundation like footings/piles).

Steel families are resolved lazily by ac_sections.SteelSectionCatalog
(it also owns the family-file lookup, since the same type-catalog scan
finds both the .rfa and its .txt in one pass); generation (M6) for both
kinds lands together.
"""
import os

import clr
clr.AddReference('RevitAPI')

from Autodesk.Revit.DB import (
    FilteredElementCollector, FamilySymbol, BuiltInCategory, StorageType,
    Material, XYZ, Line, ElementTransformUtils, BuiltInParameter,
    IFamilyLoadOptions
)
from Autodesk.Revit.DB.Structure import StructuralType

import ac_config as cfg
from pp_common.units import get_name as _name
from pp_common.geometry import norm_half_pi
from pp_common import placement as pp_placement


class ConcreteColumnFamily(object):
    """Base for a concrete column family kind: finds/loads the base
    symbol and confirms its real type-parameter names."""

    display_name = "concrete column"
    name_candidates = []
    file_candidates = []
    library_roots = []
    size_param_candidates = {}  # {'width': [...], ...} -- kind-specific

    def find_base_symbol(self, doc):
        collector = (FilteredElementCollector(doc)
                     .OfCategory(BuiltInCategory.OST_StructuralColumns)
                     .OfClass(FamilySymbol))
        wanted = [c.lower() for c in self.name_candidates]
        for sym in collector:
            try:
                if sym.Family.Name.lower() in wanted:
                    return sym
            except Exception:
                continue
        return None

    def load_family(self, doc):
        """Loads the base family from the library roots. Must be
        called inside a transaction. Returns the loaded file path or
        None."""
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
        """Finds the actual size type-parameter names on this symbol
        (they vary between family versions), plus an optional
        Structural Material parameter (None when the family has no such
        parameter at all -- not every one does). Returns {'width':
        name-or-None, ..., 'material': name-or-None}."""
        found = {key: None for key in self.size_param_candidates}
        found['material'] = None
        candidates = {k: [c.lower() for c in v]
                     for k, v in self.size_param_candidates.items()}
        for p in symbol.Parameters:
            try:
                name = p.Definition.Name.strip()
            except Exception:
                continue
            low = name.lower()
            if p.StorageType == StorageType.Double and not p.IsReadOnly:
                for key, cands in candidates.items():
                    if found[key] is None and low in cands:
                        found[key] = name
            elif p.StorageType == StorageType.ElementId and not p.IsReadOnly:
                if found['material'] is None and low in [
                        c.lower() for c in cfg.COLUMN_MATERIAL_PARAM_CANDIDATES]:
                    found['material'] = name
        return found

    def find_or_create_concrete_material(self, doc):
        """Finds a material matching CONCRETE_MATERIAL_NAME_CANDIDATES.
        Does not create one -- mirrors Auto Pile's ap_kinds.py, where
        every tested project/template already ships at least one
        concrete material. Returns the Material element, or None."""
        wanted = [c.lower() for c in cfg.CONCRETE_MATERIAL_NAME_CANDIDATES]
        fallback = None
        for m in FilteredElementCollector(doc).OfClass(Material):
            low = _name(m).lower()
            if low in wanted:
                return m
            if fallback is None and "concrete" in low:
                fallback = m
        return fallback

    def _apply_size(self, sym, params, group):
        """Subclass hook: sets this kind's size parameter(s) on the
        duplicated type from the group's resolved values."""
        raise NotImplementedError

    def _needs_rotation(self):
        """True for shapes with an orientation (rectangular); round
        columns are rotationally symmetric so never need it."""
        return False

    def generate(self, doc, level, next_above, unconn_height_ft_default, groups):
        """Creates types + instances. Must be called inside an already
        open Transaction. groups: [{'label', 'shapes', ...kind-specific
        size keys, 'unconnected_height_ft' (optional, per-type override)}].
        Returns (created_count, warnings, instances) -- instances feeds
        the M7 auto-join pass."""
        base = self.find_base_symbol(doc)
        if base is None:
            if self.load_family(doc):
                doc.Regenerate()
                base = self.find_base_symbol(doc)
        if base is None:
            raise ValueError(
                "No {} family ('{}') is loaded and none could be found in "
                "the Revit library. Load it into the project manually and "
                "try again.".format(self.display_name, self.name_candidates[0]))

        params = self.resolve_params(base)
        missing = [k for k in self.size_param_candidates if not params.get(k)]
        if missing:
            raise ValueError(
                "Family '{}' has no writable {} type parameter(s). Adjust "
                "the candidates in ac_config.py.".format(
                    base.Family.Name, ", ".join(missing)))

        material = self.find_or_create_concrete_material(doc) if params.get('material') else None

        family = base.Family
        existing = {}
        for sid in family.GetFamilySymbolIds():
            sym = doc.GetElement(sid)
            existing[_name(sym)] = sym

        created = 0
        warnings = []
        instances = []
        # Which model axis the family's Width/Length parameter runs
        # along is NOT assumed (see pp_common.placement.
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
                if material is not None and params.get('material'):
                    sym.LookupParameter(params['material']).Set(material.Id)
                if not sym.IsActive:
                    sym.Activate()
                doc.Regenerate()
            except Exception as ex:
                warnings.append(u"Type '{}': {}".format(type_name, ex))
                continue

            unconn_ft = g.get('unconnected_height_ft', unconn_height_ft_default)
            for shape in g['shapes']:
                try:
                    pt = XYZ(shape.center[0], shape.center[1], level.ProjectElevation)
                    inst = doc.Create.NewFamilyInstance(
                        pt, sym, level, StructuralType.Column)
                    doc.Regenerate()
                    if self._needs_rotation() and g.get('width_ft') is not None:
                        if length_axis is None:
                            length_axis = pp_placement.detect_length_axis(
                                doc, inst, g['width_ft'], g['length_ft'])
                        rotation = getattr(shape, 'rotation', 0.0)
                        delta = norm_half_pi(rotation - (length_axis or 0.0))
                        if abs(delta) > 1e-6:
                            axis = Line.CreateBound(pt, XYZ(pt.X, pt.Y, pt.Z + 1.0))
                            ElementTransformUtils.RotateElement(
                                doc, inst.Id, axis, delta)
                    pp_placement.apply_column_vertical_constraints(
                        doc, inst, level, next_above, unconn_ft)
                    instances.append(inst)
                    created += 1
                except Exception as ex:
                    warnings.append(u"'{}' at ({:.2f}, {:.2f}): {}".format(
                        type_name, shape.center[0], shape.center[1], ex))

        return created, warnings, instances


class RectConcreteColumnFamily(ConcreteColumnFamily):
    display_name = "rectangular concrete column"
    name_candidates = cfg.RECT_CONCRETE_FAMILY_NAME_CANDIDATES
    file_candidates = cfg.RECT_CONCRETE_FAMILY_FILE_CANDIDATES
    library_roots = cfg.CONCRETE_COLUMN_LIBRARY_ROOTS
    size_param_candidates = {
        'width': cfg.COLUMN_WIDTH_PARAM_CANDIDATES,
        'depth': cfg.COLUMN_DEPTH_PARAM_CANDIDATES,
    }

    def _apply_size(self, sym, params, group):
        sym.LookupParameter(params['width']).Set(group['width_ft'])
        sym.LookupParameter(params['depth']).Set(group['length_ft'])

    def _needs_rotation(self):
        return True


class RoundConcreteColumnFamily(ConcreteColumnFamily):
    display_name = "round concrete column"
    name_candidates = cfg.ROUND_CONCRETE_FAMILY_NAME_CANDIDATES
    file_candidates = cfg.ROUND_CONCRETE_FAMILY_FILE_CANDIDATES
    library_roots = cfg.CONCRETE_COLUMN_LIBRARY_ROOTS
    size_param_candidates = {'diameter': cfg.COLUMN_DIAMETER_PARAM_CANDIDATES}

    def _apply_size(self, sym, params, group):
        sym.LookupParameter(params['diameter']).Set(group['diameter_ft'])


class _OverwriteFamilyLoadOptions(IFamilyLoadOptions):
    """Passed to Document.LoadFamilySymbol so it will actually add a
    catalog row that is not yet among the family's loaded types --
    confirmed live: with NO IFamilyLoadOptions, LoadFamilySymbol
    silently returns False (loads nothing) whenever the family is
    already present in the project with only a PARTIAL set of its
    catalog types loaded (e.g. left over from an earlier manual
    insert), even when asked for a type that already exists. Passing
    this options object is what makes it actually load the missing
    type -- this is not a documented gotcha, it was found by testing
    against the real project."""

    def OnFamilyFound(self, familyInUse, overwriteParameterValues):
        overwriteParameterValues.Value = True
        return True

    def OnSharedFamilyFound(self, sharedFamily, familyInUse, source, overwriteParameterValues):
        source.Value = 0
        overwriteParameterValues.Value = True
        return True


class SteelColumnFamily(object):
    """Loads Revit's own type-catalog steel column families (W/C/HSS)
    and selects the exact pre-built type by name -- unlike the concrete
    families above, these are catalog families: every row of the .txt
    catalog IS a distinct family type Revit builds when the family is
    loaded, so there is no size parameter to set here, just the right
    type to find (row.Section from ac_sections' snap, fully overridable
    in the UI)."""

    display_name = "steel column"

    def _family_base_name(self, family_path):
        return os.path.splitext(os.path.basename(family_path))[0].lower()

    def find_symbol(self, doc, family_path, type_name):
        base_name = self._family_base_name(family_path)
        wanted = (type_name or "").strip().lower()
        if not wanted:
            return None
        collector = (FilteredElementCollector(doc)
                     .OfCategory(BuiltInCategory.OST_StructuralColumns)
                     .OfClass(FamilySymbol))
        for sym in collector:
            try:
                if sym.Family.Name.lower() != base_name:
                    continue
                name_param = sym.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
                name = name_param.AsString() if name_param else None
            except Exception:
                continue
            if name and name.strip().lower() == wanted:
                return sym
        return None

    def load_and_find_symbol(self, doc, family_path, type_name):
        """Must be called inside an already open Transaction. Uses
        LoadFamilySymbol (loads exactly the one named catalog row,
        adding it to the family even if that family is already partly
        loaded) rather than LoadFamily (which only brings in a
        family's default type and does nothing for an already-present
        family -- see _OverwriteFamilyLoadOptions)."""
        sym = self.find_symbol(doc, family_path, type_name)
        if sym is not None:
            return sym
        try:
            doc.LoadFamilySymbol(family_path, type_name, _OverwriteFamilyLoadOptions())
            doc.Regenerate()
        except Exception:
            pass
        return self.find_symbol(doc, family_path, type_name)

    def generate(self, doc, level, next_above, unconn_height_ft_default, groups):
        """Creates instances only (no types to duplicate). Must be
        called inside an already open Transaction. groups: [{'label',
        'family_path', 'section_name', 'shapes', 'width_ft', 'length_ft',
        'unconnected_height_ft' (optional)}]. Returns (created_count,
        warnings, instances) -- instances feeds the M7 auto-join pass."""
        created = 0
        warnings = []
        instances = []
        length_axis_by_family = {}

        for g in groups:
            if not g.get('family_path'):
                warnings.append(u"Type '{}': no steel catalog family could be "
                                u"found for this shape (see ac_config.py's "
                                u"STEEL_LIBRARY_ROOTS).".format(g['label']))
                continue
            sym = self.load_and_find_symbol(doc, g['family_path'], g['section_name'])
            if sym is None:
                warnings.append(u"Type '{}': section '{}' was not found in "
                                u"family '{}' (typo, or the catalog changed "
                                u"since scanning -- re-run Apply and check "
                                u"the Section column).".format(
                                    g['label'], g['section_name'],
                                    os.path.basename(g['family_path'])))
                continue
            try:
                if not sym.IsActive:
                    sym.Activate()
                doc.Regenerate()
            except Exception as ex:
                warnings.append(u"Type '{}': {}".format(g['label'], ex))
                continue

            unconn_ft = g.get('unconnected_height_ft', unconn_height_ft_default)
            length_axis = length_axis_by_family.get(g['family_path'])
            for shape in g['shapes']:
                try:
                    pt = XYZ(shape.center[0], shape.center[1], level.ProjectElevation)
                    inst = doc.Create.NewFamilyInstance(
                        pt, sym, level, StructuralType.Column)
                    doc.Regenerate()
                    if abs(g['width_ft'] - g['length_ft']) > 0.01:
                        if length_axis is None:
                            length_axis = pp_placement.detect_length_axis(
                                doc, inst, g['width_ft'], g['length_ft'])
                            length_axis_by_family[g['family_path']] = length_axis
                        rotation = getattr(shape, 'rotation', 0.0)
                        delta = norm_half_pi(rotation - (length_axis or 0.0))
                        if abs(delta) > 1e-6:
                            axis = Line.CreateBound(pt, XYZ(pt.X, pt.Y, pt.Z + 1.0))
                            ElementTransformUtils.RotateElement(
                                doc, inst.Id, axis, delta)
                    pp_placement.apply_column_vertical_constraints(
                        doc, inst, level, next_above, unconn_ft)
                    instances.append(inst)
                    created += 1
                except Exception as ex:
                    warnings.append(u"'{}' at ({:.2f}, {:.2f}): {}".format(
                        g['label'], shape.center[0], shape.center[1], ex))

        return created, warnings, instances


class CustomColumnFamily(object):
    """Arbitrary (L, T, or anything not recognized as a plain rectangle,
    circle, I/H, channel, or hollow tube) column profile -- NOT
    implemented yet. Matches ac_shapes.CustomColumnShape: script.py's
    on_generate already routes "custom" rows around every real kind
    above and reports them as skipped, so generate() here only needs to
    fail clearly if something ever calls it directly.

    Planned: mirror Auto Pile's CustomPileFamily treatment -- build a
    structural column element from the exact CAD outline (an in-place
    extrusion, or a dedicated custom column family once one is
    confirmed) instead of a duplicated symbol's rectangular/circular
    profile."""

    display_name = "custom column"

    def generate(self, doc, level, next_above, unconn_height_ft_default, groups):
        raise NotImplementedError(
            "Custom-profile column generation is not implemented yet.")
