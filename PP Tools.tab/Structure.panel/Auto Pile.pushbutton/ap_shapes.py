# -*- coding: utf-8 -*-
"""Pile shape classification for Auto Pile.

PileShape is the extension point for pile cross-sections:

  * CircleShape     — true CAD circle (Diameter), or a circular block
  * SquareShape     — closed polyline/block outline (Side, or Width x
                      Length when the outline isn't truly square) —
                      wraps pp_common.geometry.Footprint for its
                      oriented-bbox/rotation/point-in-polygon math
                      instead of duplicating it
  * CustomPileShape — arbitrary outline — classified but not yet
                      generated (see ap_kinds.CustomPileFamily)

All three (and Footprint itself) are duck-typed to pp_common.labels'
shape interface (contains/dist_to_boundary/dist2_to_center/width_ft/
length_ft/label), so CAD-label matching works unchanged across every
shape kind.
"""
import math

from pp_common.geometry import Footprint, chain_lines_to_loops
from pp_common.config_base import MM_TO_FT
from pp_common import labels as pp_labels


class PileShape(object):
    display_name = "pile"

    def __init__(self, center, source):
        self.center = center
        # how the shape was detected: 'circle' | 'polyline' |
        # 'block-geometry' | 'block-name' (the last is DXF-only — see
        # ap_config.py; not produced by the geometry-API-only path)
        self.source = source
        self.label = None

    def contains(self, x, y):
        raise NotImplementedError

    def dist_to_boundary(self, x, y):
        raise NotImplementedError

    def dist2_to_center(self, x, y):
        cx, cy = self.center
        return (x - cx) ** 2 + (y - cy) ** 2


class CircleShape(PileShape):
    """True CAD circle (Diameter)."""

    display_name = "circle"

    def __init__(self, center, diameter_ft, source="circle"):
        PileShape.__init__(self, center, source)
        self.diameter_ft = diameter_ft
        # width_ft/length_ft let CircleShape reuse the generic
        # size-grouping/label-matching code shared with Footprint
        self.width_ft = diameter_ft
        self.length_ft = diameter_ft

    def contains(self, x, y):
        return self.dist2_to_center(x, y) <= (self.diameter_ft / 2.0) ** 2

    def dist_to_boundary(self, x, y):
        d = math.sqrt(self.dist2_to_center(x, y))
        return max(0.0, d - self.diameter_ft / 2.0)


class SquareShape(PileShape):
    """Closed polyline or block outline: Side when the bounding box is
    (nearly) square, otherwise Width x Length. Wraps a Footprint for its
    oriented-bbox/rotation/point-in-polygon math."""

    display_name = "square"

    def __init__(self, footprint, is_square, source="polyline"):
        PileShape.__init__(self, footprint.center, source)
        self.footprint = footprint
        self.width_ft = footprint.width_ft
        self.length_ft = footprint.length_ft
        self.rotation = footprint.rotation
        self.is_square = is_square
        self.side_ft = footprint.width_ft if is_square else None

    def contains(self, x, y):
        return self.footprint.contains(x, y)

    def dist_to_boundary(self, x, y):
        return self.footprint.dist_to_boundary(x, y)


class CustomPileShape(PileShape):
    """Arbitrary (non-circle, non-rectangular) pile outline — NOT
    implemented yet.

    Planned: same treatment as Auto Foundation's CustomPadFooting (an
    exact-outline structural element) once a suitable pile/pier family
    or slab-based representation is confirmed for irregular sections."""

    display_name = "custom"

    def __init__(self, footprint, source="polyline"):
        PileShape.__init__(self, footprint.center, source)
        self.footprint = footprint
        self.width_ft = footprint.width_ft
        self.length_ft = footprint.length_ft

    def contains(self, x, y):
        return self.footprint.contains(x, y)

    def dist_to_boundary(self, x, y):
        return self.footprint.dist_to_boundary(x, y)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_polyline(points, cfg):
    """Builds a Footprint from a closed polyline/block outline and wraps
    it as SquareShape, or CustomPileShape when it isn't a clean
    rectangle at all."""
    fp = Footprint(points, rect_area_ratio=cfg.RECT_AREA_RATIO,
                  rotation_snap_deg=cfg.ROTATION_SNAP_DEG)
    if not fp.is_rectangle:
        return CustomPileShape(fp)
    square_tol_ft = cfg.SQUARE_SIDE_TOLERANCE_MM * cfg.MM_TO_FT
    is_square = abs(fp.width_ft - fp.length_ft) <= square_tol_ft
    return SquareShape(fp, is_square)


def classify_block(position, nested_geometry, cfg):
    """Classifies a block reference's shape by recursing into its own
    geometry (method b from the spec — method a, block-name detection,
    isn't available through Revit's geometry API; see ap_config.py).
    Prefers a circle if the block contains one, else the largest closed
    polyline/line-loop outline found inside it. Returns a PileShape
    subclass positioned at the block's insertion point, or None if
    nothing recognizable was found."""
    from Autodesk.Revit.DB import GeometryInstance, PolyLine, Line, Arc

    circles = []
    polylines = []
    lines = []

    def walk(g):
        if g is None:
            return
        for obj in g:
            if isinstance(obj, GeometryInstance):
                try:
                    walk(obj.GetInstanceGeometry())
                except Exception:
                    pass
                continue
            if isinstance(obj, Arc):
                if not obj.IsBound:
                    c = obj.Center
                    circles.append((c.X, c.Y, obj.Radius))
            elif isinstance(obj, PolyLine):
                try:
                    polylines.append([(c.X, c.Y) for c in obj.GetCoordinates()])
                except Exception:
                    pass
            elif isinstance(obj, Line):
                p0, p1 = obj.GetEndPoint(0), obj.GetEndPoint(1)
                lines.append(((p0.X, p0.Y), (p1.X, p1.Y)))

    walk(nested_geometry)

    if circles:
        _cx, _cy, r = max(circles, key=lambda c: c[2])
        return CircleShape(position, r * 2.0, source="block-geometry")

    tol_ft = cfg.CHAIN_TOLERANCE_MM * cfg.MM_TO_FT
    outlines = list(polylines)
    outlines.extend(chain_lines_to_loops(lines, tol_ft))
    if outlines:
        largest = max(outlines, key=lambda pts: Footprint(pts).area)
        shape = classify_polyline(largest, cfg)
        shape.center = position
        shape.source = "block-geometry"
        return shape

    return None


def default_pile_label(shape):
    """Size-derived fallback name used when no CAD label is close enough
    to trust for this specific shape. Piles intentionally never adopt a
    same-size shape's label the way Auto Foundation does for footings
    (see match_and_group): many identical-diameter piles commonly sit
    under one labelled pile CAP, and that cap label doesn't describe
    the individual pile, so borrowing it is misleading rather than
    helpful. A size-derived name (D800, S800, 800x1200) is more useful
    here than either a borrowed label or a bare X1/X2."""
    def mm(value_ft):
        return int(round(value_ft / MM_TO_FT))
    if isinstance(shape, CircleShape):
        return u"D{}".format(mm(shape.diameter_ft))
    if isinstance(shape, SquareShape) and shape.is_square:
        return u"S{}".format(mm(shape.side_ft))
    return u"{}x{}".format(mm(shape.width_ft), mm(shape.length_ft))


def group_by_size_only(shapes):
    """No-label mode: skip CAD text matching entirely and group purely
    by each shape's own derived size (diameter for circles; side, or
    WxL, for squares/custom) via default_pile_label. Used when the CAD
    drawing has no usable label layer at all -- for a well-behaved pile
    drawing this is often just as good as label matching, since piles
    are naturally typed by size anyway.

    Returns OrderedDict label -> [shape], same shape as match_and_group's
    second return value."""
    for s in shapes:
        s.label = default_pile_label(s)
    return pp_labels.group_shapes(shapes)


def match_and_group(shapes, texts, cfg):
    """Matches CAD labels to pile shapes and groups by label.

    Unlike Auto Foundation, piles never adopt a same-size shape's label
    (adopt_same_size_label=False) — confirmed against a real model
    where 99 unrelated 800mm bore piles would otherwise have inherited
    one borrowed pile-cap label. Shapes with no trustworthy nearby CAD
    text instead get a size-derived name via default_pile_label.

    Returns (matched_count, OrderedDict label -> [shape])."""
    matched = pp_labels.match_labels(
        shapes, texts,
        label_max_distance_mm=cfg.LABEL_MAX_DISTANCE_MM,
        size_group_round_mm=cfg.SIZE_GROUP_ROUND_MM,
        adopt_same_size_label=False,
        synthetic_label_fn=default_pile_label)
    groups = pp_labels.group_shapes(shapes)
    return matched, groups


def scan_perimeter_layer(doc, cad_import, perim_cat, cfg):
    """Classifies every pile candidate on one perimeter layer
    (perim_cat may be a single Category or a LayerSelection spanning
    several real layers). Returns (shapes, report) where report is a
    dict of diagnostic counts ('circles', 'square_polylines',
    'rect_polylines', 'custom_polylines', 'blocks_circle',
    'blocks_square', 'blocks_rect', 'blocks_custom',
    'blocks_unresolved')."""
    from Autodesk.Revit.DB import Options
    from pp_common.cad_read import collect_shape_entities

    opts = Options()
    opts.IncludeNonVisibleObjects = True
    geom = cad_import.get_Geometry(opts)

    buckets = collect_shape_entities(doc, geom, perim_cat)

    shapes = []
    report = {
        'circles': 0, 'square_polylines': 0, 'rect_polylines': 0,
        'custom_polylines': 0, 'blocks_circle': 0, 'blocks_square': 0,
        'blocks_rect': 0, 'blocks_custom': 0, 'blocks_unresolved': 0,
    }

    for cx, cy, r in buckets['circles']:
        shapes.append(CircleShape((cx, cy), r * 2.0, source="circle"))
        report['circles'] += 1

    tol_ft = cfg.CHAIN_TOLERANCE_MM * cfg.MM_TO_FT
    outlines = []
    for pts in buckets['polylines']:
        if len(pts) >= 2 and (abs(pts[0][0] - pts[-1][0]) <= tol_ft
                              and abs(pts[0][1] - pts[-1][1]) <= tol_ft):
            pts = pts[:-1]
        if len(pts) >= 3:
            outlines.append(pts)
    outlines.extend(chain_lines_to_loops(buckets['lines'], tol_ft))

    min_side = cfg.MIN_FOOTPRINT_SIDE_MM * cfg.MM_TO_FT
    max_side = cfg.MAX_FOOTPRINT_SIDE_MM * cfg.MM_TO_FT
    for pts in outlines:
        shape = classify_polyline(pts, cfg)
        if shape.width_ft < min_side or shape.length_ft > max_side:
            continue
        shapes.append(shape)
        if isinstance(shape, SquareShape):
            report['square_polylines' if shape.is_square else 'rect_polylines'] += 1
        else:
            report['custom_polylines'] += 1

    for block in buckets['blocks']:
        shape = classify_block(block['position'], block['geometry'], cfg)
        if shape is None:
            report['blocks_unresolved'] += 1
            continue
        shapes.append(shape)
        if isinstance(shape, CircleShape):
            report['blocks_circle'] += 1
        elif isinstance(shape, SquareShape):
            report['blocks_square' if shape.is_square else 'blocks_rect'] += 1
        else:
            report['blocks_custom'] += 1

    return shapes, report
