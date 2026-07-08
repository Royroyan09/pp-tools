# -*- coding: utf-8 -*-
"""Level helpers shared by the auto-modelling tools.

Added for Auto Column's vertical constraints (base = the column's
level, top = the NEXT level above, else unconnected height), but
generic: any tool needing sorted levels or an above/below lookup uses
these instead of re-implementing the collector + sort.
"""
import clr
clr.AddReference('RevitAPI')

from Autodesk.Revit.DB import FilteredElementCollector, Level


def list_levels_sorted(doc):
    """All levels in the document, sorted by elevation ascending."""
    levels = list(FilteredElementCollector(doc).OfClass(Level).ToElements())
    levels.sort(key=lambda lv: lv.Elevation)
    return levels


def next_level_above(doc, level, min_gap_ft=1e-6):
    """The nearest level strictly above the given one, or None when it
    is the topmost. min_gap_ft guards against duplicate-elevation levels
    (two levels at the same height should not count as 'above' each
    other)."""
    best = None
    for lv in list_levels_sorted(doc):
        if lv.Elevation > level.Elevation + min_gap_ft:
            if best is None or lv.Elevation < best.Elevation:
                best = lv
    return best
