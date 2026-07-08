# -*- coding: utf-8 -*-
"""Auto-join pass for Auto Column.

After columns are placed, join them against nearby structural floors and
structural framing so views/schedules/exports read a continuous
structural model instead of visually-overlapping but geometrically
independent solids.

Confirmed join order (see the user's spec): FLOOR wins over a concrete
column (the floor is the cutting element -- it is poured through/around
the column, not the reverse). Column wins over FRAMING (the column cuts
the beam). Every join attempt is wrapped in its own try/except: Revit
refusing a join (non-intersecting geometry, incompatible categories,
duplicate/overlapping instances, etc.) is a normal, expected outcome for
some columns -- not a fatal error -- and is reported as a skip rather
than aborting the whole pass or surfacing as a hard failure.

Steel-column join is off by default (cfg.ENABLE_STEEL_COLUMN_JOIN) --
the user has not confirmed steel join behavior yet (no steel CAD data
was available to test against during M4-M6 either); concrete join is
unconditional since floor/framing interaction for concrete columns was
explicitly specified.
"""
import clr
clr.AddReference('RevitAPI')

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter,
    JoinGeometryUtils, BoundingBoxIntersectsFilter, Outline, XYZ
)

import ac_config as cfg


def _is_structural_floor(floor):
    """A Floor element with its 'Structural' checkbox on. Missing/
    unreadable parameter -> treated as NOT structural (never guess;
    an architectural slab joining a column is not what was asked
    for)."""
    try:
        p = floor.get_Parameter(BuiltInParameter.FLOOR_PARAM_IS_STRUCTURAL)
        return p is not None and p.AsInteger() == 1
    except Exception:
        return False


def _nearby_elements(doc, inst, category, margin_ft):
    """Elements of category whose bounding box intersects inst's own
    bounding box expanded by margin_ft on every side -- cheap spatial
    prefilter so the join pass does not test every column against
    every floor/beam in the model."""
    bb = inst.get_BoundingBox(None)
    if bb is None:
        return []
    outline = Outline(
        XYZ(bb.Min.X - margin_ft, bb.Min.Y - margin_ft, bb.Min.Z - margin_ft),
        XYZ(bb.Max.X + margin_ft, bb.Max.Y + margin_ft, bb.Max.Z + margin_ft))
    return list(FilteredElementCollector(doc)
               .OfCategory(category)
               .WhereElementIsNotElementType()
               .WherePasses(BoundingBoxIntersectsFilter(outline))
               .ToElements())


def _join_pair(doc, cutter, cuttee):
    """Joins cutter/cuttee and makes sure cutter ends up as the cutting
    element. Returns True if the pair ends the call joined with the
    intended cutting order, False if Revit refused the join outright
    (not an error -- e.g. the two solids don't actually intersect)."""
    try:
        if not JoinGeometryUtils.AreElementsJoined(doc, cutter, cuttee):
            JoinGeometryUtils.JoinGeometry(doc, cutter, cuttee)
        if not JoinGeometryUtils.IsCuttingElementInJoin(doc, cutter, cuttee):
            JoinGeometryUtils.SwitchJoinOrder(doc, cutter, cuttee)
        return True
    except Exception:
        return False


def join_columns_to_floors_and_framing(doc, instances, margin_ft=1.0):
    """instances: placed column FamilyInstance elements (concrete, or
    steel when the caller has confirmed cfg.ENABLE_STEEL_COLUMN_JOIN).
    Joins each against nearby structural floors (floor cuts column) and
    structural framing (column cuts framing). Returns (joined_count,
    skipped_count) -- skipped is a normal outcome (see _join_pair), not
    a warning-worthy failure, but is reported to the user for
    transparency."""
    joined = 0
    skipped = 0
    for inst in instances:
        for floor in _nearby_elements(doc, inst, BuiltInCategory.OST_Floors, margin_ft):
            if not _is_structural_floor(floor):
                continue
            if _join_pair(doc, floor, inst):
                joined += 1
            else:
                skipped += 1
        for beam in _nearby_elements(doc, inst, BuiltInCategory.OST_StructuralFraming, margin_ft):
            if _join_pair(doc, inst, beam):
                joined += 1
            else:
                skipped += 1
    return joined, skipped
