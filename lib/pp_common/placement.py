# -*- coding: utf-8 -*-
"""Placement helpers shared by every auto-modelling tool."""


def detect_length_axis(doc, inst, width_ft, length_ft):
    """Measures a freshly placed, unrotated instance to find which model
    axis the family's Length (or long-side) parameter runs along.
    Returns 0.0 (along X), pi/2 (along Y), or None when it cannot be
    determined (square instance, no bounding box). The family's own
    orientation convention is never assumed — it differs between family
    versions/authors — so every rotation a caller applies is taken
    relative to this measured axis rather than a hard-coded one."""
    import math
    if abs(length_ft - width_ft) < 0.01:
        return None
    try:
        doc.Regenerate()
        bb = inst.get_BoundingBox(None)
        if bb is None:
            return None
        dx = bb.Max.X - bb.Min.X
        dy = bb.Max.Y - bb.Min.Y
        if abs(dx - dy) < 0.01:
            return None
        return 0.0 if dx > dy else math.pi / 2.0
    except Exception:
        return None


def apply_column_vertical_constraints(doc, inst, level, next_above, unconnected_height_ft):
    """Base = level, offset 0. Top = the next level above (offset 0)
    when one exists; otherwise the column is left "unconnected" by
    pointing its own Top Level parameter back at its base level with a
    nonzero Top Offset -- this is not an assumption, it is exactly how
    Revit itself represents an unconnected column (confirmed live:
    placing a column at the topmost level with no level above, Revit
    defaults Top Level to that same level with a nonzero Top Offset)."""
    from Autodesk.Revit.DB import BuiltInParameter
    inst.get_Parameter(BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM).Set(0.0)
    if next_above is not None:
        inst.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM).Set(next_above.Id)
        inst.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM).Set(0.0)
    else:
        inst.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_PARAM).Set(level.Id)
        inst.get_Parameter(BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM).Set(unconnected_height_ft)


def flush_top_with_level(doc, elements, elev, offset_bip, tolerance_ft=0.001):
    """The family/type origin behaviour is not assumed: measures each
    placed element's top face and corrects its level offset so the TOP
    sits flush with the level, thickness/depth extending down. Returns a
    list of warnings."""
    warnings = []
    doc.Regenerate()
    for el in elements:
        try:
            bb = el.get_BoundingBox(None)
            if bb is None:
                continue
            dz = elev - bb.Max.Z
            if abs(dz) > tolerance_ft:
                off = el.get_Parameter(offset_bip)
                if off is not None and not off.IsReadOnly:
                    off.Set(off.AsDouble() + dz)
        except Exception as ex:
            warnings.append(u"Top-flush check failed: {}".format(ex))
    return warnings
