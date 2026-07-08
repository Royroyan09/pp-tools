# -*- coding: utf-8 -*-
"""Structural framing family resolution + placement for Auto Beam (M7).

Every prior tool places a family at a POINT (NewFamilyInstance(pt, sym,
level, StructuralType.X)). A beam is placed along a CURVE instead
(NewFamilyInstance(curve, sym, level, StructuralType.Beam)) -- there is
no rotation-axis detection to do here (the curve itself sets the
beam's horizontal direction), but there IS a vertical placement rule
none of the point-placed tools needed: the spec calls for the beam's
TOP face flush with the selected level (Z Justification = Top), not
its centerline or base.

Z Justification's real enum is confirmed at runtime (Autodesk.Revit.
DB.Structure.ZJustification.Top) rather than assumed, exactly like
every other "confirm the real parameter/enum" step elsewhere in this
extension. But the enum lookup is only the FIRST attempt -- the actual
flush is still verified/corrected by measuring each placed instance's
bounding box and adjusting its Z Offset Value (pp_common.placement.
flush_top_with_level, already shared and already used by Auto
Foundation/Auto Pile), so a wrong justification-enum guess would still
self-correct rather than silently placing beams at the wrong height.
"""
import os

import clr
clr.AddReference('RevitAPI')

from Autodesk.Revit.DB import (
    FilteredElementCollector, FamilySymbol, BuiltInCategory, StorageType,
    Material, XYZ, Line, BuiltInParameter, IFamilyLoadOptions
)
from Autodesk.Revit.DB.Structure import StructuralType

import ab_config as cfg
from pp_common.units import get_name as _name
from pp_common import placement as pp_placement


def _try_set_top_justification(inst):
    """Best-effort: sets Z Justification to Top via the real Revit
    enum (not a hard-coded int). Returns True on success -- callers
    never depend on this succeeding, since the bounding-box flush below
    corrects the actual height regardless."""
    try:
        import Autodesk.Revit.DB.Structure as DBStructure
        p = inst.get_Parameter(BuiltInParameter.Z_JUSTIFICATION)
        if p is None or p.IsReadOnly:
            return False
        p.Set(int(DBStructure.ZJustification.Top))
        return True
    except Exception:
        return False


class ConcreteBeamFamily(object):
    """Rectangular concrete structural framing family: finds/loads the
    base symbol, confirms its real b/h type-parameter names, duplicates
    one type per label, and places one curve-based instance per
    continuous span."""

    display_name = "rectangular concrete beam"
    name_candidates = cfg.RECT_CONCRETE_BEAM_FAMILY_NAME_CANDIDATES
    file_candidates = cfg.RECT_CONCRETE_BEAM_FAMILY_FILE_CANDIDATES
    library_roots = cfg.CONCRETE_BEAM_LIBRARY_ROOTS

    def find_base_symbol(self, doc):
        collector = (FilteredElementCollector(doc)
                     .OfCategory(BuiltInCategory.OST_StructuralFraming)
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
        """Finds the actual b/h type-parameter names on this symbol
        (they vary between family versions), plus an optional
        Structural Material parameter. Returns {'width': name-or-None,
        'depth': name-or-None, 'material': name-or-None}."""
        found = {'width': None, 'depth': None, 'material': None}
        width_cands = [c.lower() for c in cfg.BEAM_WIDTH_PARAM_CANDIDATES]
        depth_cands = [c.lower() for c in cfg.BEAM_DEPTH_PARAM_CANDIDATES]
        for p in symbol.Parameters:
            try:
                name = p.Definition.Name.strip()
            except Exception:
                continue
            low = name.lower()
            if p.StorageType == StorageType.Double and not p.IsReadOnly:
                if found['width'] is None and low in width_cands:
                    found['width'] = name
                elif found['depth'] is None and low in depth_cands:
                    found['depth'] = name
            elif p.StorageType == StorageType.ElementId and not p.IsReadOnly:
                if found['material'] is None and low in [
                        c.lower() for c in cfg.BEAM_MATERIAL_PARAM_CANDIDATES]:
                    found['material'] = name
        return found

    def find_or_create_concrete_material(self, doc):
        """Finds a material matching CONCRETE_MATERIAL_NAME_CANDIDATES.
        Does not create one -- every tested project/template already
        ships at least one concrete material. Returns the Material
        element, or None."""
        wanted = [c.lower() for c in cfg.CONCRETE_MATERIAL_NAME_CANDIDATES]
        fallback = None
        for m in FilteredElementCollector(doc).OfClass(Material):
            low = _name(m).lower()
            if low in wanted:
                return m
            if fallback is None and "concrete" in low:
                fallback = m
        return fallback

    def generate(self, doc, level, groups):
        """Creates types + instances. Must be called inside an already
        open Transaction. groups: [{'label', 'width_ft', 'depth_ft',
        'shapes' ([BeamSpanShape,...])}]. Returns (created_count,
        warnings, instances) -- instances feeds the M8 auto-join pass."""
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
        missing = [k for k in ('width', 'depth') if not params.get(k)]
        if missing:
            raise ValueError(
                "Family '{}' has no writable {} type parameter(s). Adjust "
                "BEAM_WIDTH_PARAM_CANDIDATES/BEAM_DEPTH_PARAM_CANDIDATES in "
                "ab_config.py.".format(base.Family.Name, ", ".join(missing)))

        material = self.find_or_create_concrete_material(doc) if params.get('material') else None

        family = base.Family
        existing = {}
        for sid in family.GetFamilySymbolIds():
            sym = doc.GetElement(sid)
            existing[_name(sym)] = sym

        created = 0
        warnings = []
        instances = []
        elev = level.ProjectElevation + cfg.BEAM_Z_OFFSET_FT

        for g in groups:
            type_name = cfg.TYPE_NAME_FORMAT.format(label=g['label'])
            try:
                sym = existing.get(type_name)
                if sym is None:
                    sym = base.Duplicate(type_name)
                    existing[type_name] = sym
                sym.LookupParameter(params['width']).Set(g['width_ft'])
                sym.LookupParameter(params['depth']).Set(g['depth_ft'])
                if material is not None and params.get('material'):
                    sym.LookupParameter(params['material']).Set(material.Id)
                if not sym.IsActive:
                    sym.Activate()
                doc.Regenerate()
            except Exception as ex:
                warnings.append(u"Type '{}': {}".format(type_name, ex))
                continue

            group_instances = []
            for shape in g['shapes']:
                seg = shape.segment
                try:
                    p0 = XYZ(seg.p0[0], seg.p0[1], level.ProjectElevation)
                    p1 = XYZ(seg.p1[0], seg.p1[1], level.ProjectElevation)
                    curve = Line.CreateBound(p0, p1)
                    inst = doc.Create.NewFamilyInstance(
                        curve, sym, level, StructuralType.Beam)
                    _try_set_top_justification(inst)
                    group_instances.append(inst)
                    instances.append(inst)
                    created += 1
                except Exception as ex:
                    warnings.append(u"'{}' at ({:.2f}, {:.2f})-({:.2f}, {:.2f}): "
                                    u"{}".format(type_name, seg.p0[0], seg.p0[1],
                                                seg.p1[0], seg.p1[1], ex))

            warnings.extend(pp_placement.flush_top_with_level(
                doc, group_instances, elev, BuiltInParameter.Z_OFFSET_VALUE))

        return created, warnings, instances


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


class _CatalogBeamFamily(object):
    """Shared base for a beam kind whose family is a Revit type
    catalog (every row of the .txt catalog IS a distinct family type
    Revit builds when the family is loaded, so there is no size
    parameter to set here, just the right pre-built type to find --
    row.Section from ab_sections' snap, fully overridable in the UI).
    Steel (W/C/HSS, multiple catalogs dispatched by label) and timber
    (a single M_Timber catalog) both use exactly this pattern."""

    display_name = "catalog beam"
    missing_catalog_hint = ""

    def _family_base_name(self, family_path):
        return os.path.splitext(os.path.basename(family_path))[0].lower()

    def find_symbol(self, doc, family_path, type_name):
        base_name = self._family_base_name(family_path)
        wanted = (type_name or "").strip().lower()
        if not wanted:
            return None
        collector = (FilteredElementCollector(doc)
                     .OfCategory(BuiltInCategory.OST_StructuralFraming)
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

    def generate(self, doc, level, groups):
        """Creates instances only (no types to duplicate). Must be
        called inside an already open Transaction. groups: [{'label',
        'family_path', 'section_name', 'shapes'}]. Returns
        (created_count, warnings, instances) -- instances feeds the M8
        auto-join pass."""
        created = 0
        warnings = []
        instances = []
        elev = level.ProjectElevation + cfg.BEAM_Z_OFFSET_FT

        for g in groups:
            if not g.get('family_path'):
                warnings.append(u"Type '{}': no catalog family could be found "
                                u"for this label ({}).".format(
                                    g['label'], self.missing_catalog_hint))
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

            group_instances = []
            for shape in g['shapes']:
                seg = shape.segment
                try:
                    p0 = XYZ(seg.p0[0], seg.p0[1], level.ProjectElevation)
                    p1 = XYZ(seg.p1[0], seg.p1[1], level.ProjectElevation)
                    curve = Line.CreateBound(p0, p1)
                    inst = doc.Create.NewFamilyInstance(
                        curve, sym, level, StructuralType.Beam)
                    _try_set_top_justification(inst)
                    group_instances.append(inst)
                    instances.append(inst)
                    created += 1
                except Exception as ex:
                    warnings.append(u"'{}' at ({:.2f}, {:.2f})-({:.2f}, {:.2f}): "
                                    u"{}".format(g['label'], seg.p0[0], seg.p0[1],
                                                seg.p1[0], seg.p1[1], ex))

            warnings.extend(pp_placement.flush_top_with_level(
                doc, group_instances, elev, BuiltInParameter.Z_OFFSET_VALUE))

        return created, warnings, instances


class SteelBeamFamily(_CatalogBeamFamily):
    display_name = "steel beam"
    missing_catalog_hint = "see ab_config.py's BEAM_STEEL_LIBRARY_ROOTS"


class TimberBeamFamily(_CatalogBeamFamily):
    display_name = "timber beam"
    missing_catalog_hint = "see ab_config.py's TIMBER_LIBRARY_ROOTS"
