# -*- coding: utf-8 -*-
"""Auto-join pass for Auto Beam (M8).

After beams are placed, join them against nearby structural columns
and structural floors so views/schedules/exports read a continuous
structural model instead of visually-overlapping but geometrically
independent solids.

Confirmed hierarchy per the spec: FLOOR > COLUMN > BEAM. Floor cuts a
concrete beam (floor is poured through/around it). Column cuts a
concrete beam -- the exact reciprocal of Auto Column's own rule, where
the column is the cutting element against framing (ac_join.py's
join_columns_to_floors_and_framing already does "column cuts beam"
from the column's side; this module does the same join from the
beam's side, unconditionally for concrete). Every join attempt is
wrapped in its own try/except: Revit refusing a join (non-intersecting
geometry, incompatible categories, an already-conflicting join, etc.)
is a normal, expected outcome for some beams -- not a fatal error --
and is reported as a skip rather than aborting the whole pass.

Steel/timber beam join and beam-to-beam join are NOT specified in the
confirmed decisions (no steel/timber CAD data was available to test
against, same caveat Auto Column already carries for its own steel
join) -- both stay off by default behind ab_config.py's
ENABLE_STEEL_BEAM_JOIN / ENABLE_TIMBER_BEAM_JOIN / ENABLE_BEAM_BEAM_JOIN.
"""
import clr
clr.AddReference('RevitAPI')

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter,
    JoinGeometryUtils, BoundingBoxIntersectsFilter, Outline, XYZ
)


def _is_structural_floor(floor):
    """A Floor element with its 'Structural' checkbox on. Missing/
    unreadable parameter -> treated as NOT structural (never guess; an
    architectural slab joining a beam is not what was asked for)."""
    try:
        p = floor.get_Parameter(BuiltInParameter.FLOOR_PARAM_IS_STRUCTURAL)
        return p is not None and p.AsInteger() == 1
    except Exception:
        return False


def _nearby_elements(doc, inst, category, margin_ft):
    """Elements of category whose bounding box intersects inst's own
    bounding box expanded by margin_ft on every side -- cheap spatial
    prefilter so the join pass does not test every beam against every
    column/floor/other beam in the model."""
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


def _join_pair_either_order(doc, a, b):
    """Joins a/b without forcing a cutting winner -- used only for
    beam-to-beam join, where the spec does not specify which member
    should cut the other (unlike floor>column>beam, which is
    explicit). Returns True if the pair ends the call joined, False if
    Revit refused outright."""
    try:
        if not JoinGeometryUtils.AreElementsJoined(doc, a, b):
            JoinGeometryUtils.JoinGeometry(doc, a, b)
        return True
    except Exception:
        return False


def join_beams_to_columns_and_floors(doc, concrete_instances, steel_instances,
                                     timber_instances, cfg, margin_ft=1.0):
    """concrete_instances: placed concrete beam FamilyInstance elements
    (always join-tested, per the confirmed spec). steel_instances/
    timber_instances: only join-tested when their OWN matching
    ab_config flag is on (independently -- turning one on does not
    pull the other in). Joins each against nearby structural columns
    (column cuts beam) and structural floors (floor cuts beam), then
    -- if ENABLE_BEAM_BEAM_JOIN -- every pair of nearby beams among ALL
    of them (no forced cutting order). Returns (joined_count,
    skipped_count) -- skipped is a normal outcome (see _join_pair), not
    a warning-worthy failure, but is reported to the user for
    transparency."""
    joined = 0
    skipped = 0

    join_targets = list(concrete_instances)
    if cfg.ENABLE_STEEL_BEAM_JOIN:
        join_targets = join_targets + list(steel_instances)
    if cfg.ENABLE_TIMBER_BEAM_JOIN:
        join_targets = join_targets + list(timber_instances)

    for inst in join_targets:
        for column in _nearby_elements(doc, inst, BuiltInCategory.OST_StructuralColumns, margin_ft):
            if _join_pair(doc, column, inst):
                joined += 1
            else:
                skipped += 1
        for floor in _nearby_elements(doc, inst, BuiltInCategory.OST_Floors, margin_ft):
            if not _is_structural_floor(floor):
                continue
            if _join_pair(doc, floor, inst):
                joined += 1
            else:
                skipped += 1

    if cfg.ENABLE_BEAM_BEAM_JOIN:
        all_beams = list(concrete_instances) + list(steel_instances) + list(timber_instances)
        seen_pairs = set()
        for inst in all_beams:
            for other in _nearby_elements(doc, inst, BuiltInCategory.OST_StructuralFraming, margin_ft):
                if other.Id == inst.Id:
                    continue
                # ElementId.IntegerValue was removed in newer Revit API
                # versions (Int64 Value replaced it) -- avoid depending
                # on either by keying on a frozenset of the ElementId
                # objects themselves (hashable/comparable natively).
                key = frozenset((inst.Id, other.Id))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                if _join_pair_either_order(doc, inst, other):
                    joined += 1
                else:
                    skipped += 1

    return joined, skipped
