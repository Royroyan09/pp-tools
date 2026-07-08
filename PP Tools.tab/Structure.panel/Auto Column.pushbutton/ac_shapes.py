# -*- coding: utf-8 -*-
"""Column shape classification for Auto Column.

ColumnShape is the extension point for column cross-sections:

  * CircleColumnShape  — true CAD circle -> concrete round
  * RectColumnShape    — plain rectangle -> AMBIGUOUS by shape alone
                         (concrete rect vs. hollow steel read at the
                         wrong scale); material_hint is None, so the
                         label prefix (SECONDARY->PRIMARY reconciled in
                         reconcile_material) or a manual pick decides
  * IHColumnShape       — wide-flange/H outline (bilateral symmetry
                         about both oriented-bbox axes) -> steel
  * ChannelColumnShape  — C-channel outline (symmetric about exactly
                         one axis) -> steel
  * HollowColumnShape   — two concentric outlines with a real wall gap
                         -> steel tube, sized from the OUTER profile
  * CustomColumnShape   — L/T/anything else — NOT implemented yet
  * SymbolicSteelColumnShape — no real footprint at all, only a small
                         non-scale marker glyph (e.g. a stylized H/I
                         wide-flange icon) on a dedicated layer -> steel,
                         unconditionally (see scan_symbol_layer)

All shapes are duck-typed to pp_common.labels' interface (contains/
dist_to_boundary/dist2_to_center/width_ft/length_ft/label), reusing
pp_common.geometry.Footprint for the actual polygon math rather than
duplicating it, exactly like Auto Pile's ap_shapes.py.
"""
import math
import re

from pp_common.geometry import Footprint, chain_lines_to_loops
from pp_common.config_base import MM_TO_FT
from pp_common import labels as pp_labels


class ColumnShape(object):
    display_name = "column"
    material_hint = None  # 'concrete' | 'steel' | None (ambiguous by shape)

    def __init__(self, center, source):
        self.center = center
        self.source = source
        self.label = None

    def dist2_to_center(self, x, y):
        cx, cy = self.center
        return (x - cx) ** 2 + (y - cy) ** 2

    def contains(self, x, y):
        raise NotImplementedError

    def dist_to_boundary(self, x, y):
        raise NotImplementedError


class CircleColumnShape(ColumnShape):
    display_name = "circle"
    material_hint = "concrete"

    def __init__(self, center, diameter_ft, source="circle"):
        ColumnShape.__init__(self, center, source)
        self.diameter_ft = diameter_ft
        self.width_ft = diameter_ft
        self.length_ft = diameter_ft

    def contains(self, x, y):
        return self.dist2_to_center(x, y) <= (self.diameter_ft / 2.0) ** 2

    def dist_to_boundary(self, x, y):
        d = math.sqrt(self.dist2_to_center(x, y))
        return max(0.0, d - self.diameter_ft / 2.0)


class _FootprintColumnShape(ColumnShape):
    """Shared plumbing for every polygon-outline column shape: wraps a
    Footprint for its oriented-bbox/rotation/point-in-polygon math."""

    display_name = "profile"

    def __init__(self, footprint, source="polyline"):
        ColumnShape.__init__(self, footprint.center, source)
        self.footprint = footprint
        self.width_ft = footprint.width_ft
        self.length_ft = footprint.length_ft
        self.rotation = footprint.rotation

    def contains(self, x, y):
        return self.footprint.contains(x, y)

    def dist_to_boundary(self, x, y):
        return self.footprint.dist_to_boundary(x, y)


class RectColumnShape(_FootprintColumnShape):
    """Plain rectangle — AMBIGUOUS by shape alone. material_hint is
    None on purpose: reconcile_material() falls back to the label
    prefix, and to a manual pick when the label doesn't help either."""
    display_name = "rect"
    material_hint = None


class IHColumnShape(_FootprintColumnShape):
    """Wide-flange / H-section outline."""
    display_name = "I/H"
    material_hint = "steel"


class ChannelColumnShape(_FootprintColumnShape):
    """C-channel outline."""
    display_name = "channel"
    material_hint = "steel"


class HollowColumnShape(_FootprintColumnShape):
    """Hollow square/rectangular steel tube: two concentric outlines
    (outer + inner) with a real wall gap. Sized from the OUTER profile;
    wall thickness kept for reference/reporting."""
    display_name = "hollow"
    material_hint = "steel"

    def __init__(self, outer_footprint, inner_footprint):
        _FootprintColumnShape.__init__(self, outer_footprint, source="polyline")
        self.inner_footprint = inner_footprint
        self.wall_ft = min(
            (outer_footprint.width_ft - inner_footprint.width_ft) / 2.0,
            (outer_footprint.length_ft - inner_footprint.length_ft) / 2.0)


class CustomColumnShape(_FootprintColumnShape):
    """Arbitrary outline (L, T, or anything not recognized as a plain
    rectangle, I/H, channel, or hollow tube) — NOT implemented yet.

    Planned: same treatment as Auto Foundation's CustomPadFooting / Auto
    Pile's CustomPileShape — flagged and skipped at generation time with
    a clear message, extension point for a custom family or exact-
    outline element once one is confirmed."""
    display_name = "custom"
    material_hint = None


class SymbolicSteelColumnShape(ColumnShape):
    """A steel column with NO real, to-scale footprint drawn anywhere --
    only a small non-scale marker glyph (confirmed against a real
    drawing: a stylized wide-flange "H/I" icon a few hundred mm across,
    unrelated to the column's actual section size) plus its label. The
    real section is defined elsewhere (a schedule), not by the plan
    outline, so width_ft/length_ft are left None -- there is nothing
    meaningful to size from. material_hint is unconditionally 'steel':
    being marked on this dedicated symbol layer at all is direct
    evidence, not a guess (reconcile_material still runs normally, so
    an explicit conflicting label is still surfaced rather than
    silently overridden).

    rotation IS meaningful, though, even without a real size: the
    marker glyph's own individual plate fragments are drawn rotated to
    match the column's real orientation (confirmed against a real
    drawing with columns following a curved building edge -- each
    marker's rotation matched its local tangent exactly), so
    scan_symbol_layer reads it directly off the glyph's own geometry
    rather than leaving it at the family's default 0."""
    display_name = "symbol"
    material_hint = "steel"

    def __init__(self, center, source="symbol", rotation=0.0):
        ColumnShape.__init__(self, center, source)
        self.width_ft = None
        self.length_ft = None
        self.rotation = rotation

    def contains(self, x, y):
        return False

    def dist_to_boundary(self, x, y):
        return math.sqrt(self.dist2_to_center(x, y))


# ---------------------------------------------------------------------------
# Shape-signature classification (SECONDARY material cue)
# ---------------------------------------------------------------------------

def _sample_boundary_points(points, n):
    """n points evenly spaced by arc length around the closed outline."""
    m = len(points)
    segs = []
    total = 0.0
    for i in range(m):
        a, b = points[i], points[(i + 1) % m]
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        segs.append((a, b, d))
        total += d
    if total < 1e-9:
        return []
    step = total / n
    samples = []
    target = 0.0
    idx = 0
    acc = 0.0
    for _ in range(n):
        while idx < len(segs) - 1 and acc + segs[idx][2] < target:
            acc += segs[idx][2]
            idx += 1
        a, b, d = segs[idx]
        t = 0.0 if d < 1e-9 else max(0.0, min(1.0, (target - acc) / d))
        samples.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
        target += step
    return samples


def _edge_distance(points, x, y):
    """Distance from (x, y) to the nearest edge of the polygon (does
    NOT special-case inside/outside — unlike Footprint.dist_to_boundary,
    a symmetry test needs the raw distance-to-curve either way)."""
    best = None
    n = len(points)
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        vx, vy = x1 - x0, y1 - y0
        seg2 = vx * vx + vy * vy
        if seg2 < 1e-12:
            d2 = (x - x0) ** 2 + (y - y0) ** 2
        else:
            t = max(0.0, min(1.0, ((x - x0) * vx + (y - y0) * vy) / seg2))
            dx, dy = x0 + t * vx - x, y0 + t * vy - y
            d2 = dx * dx + dy * dy
        if best is None or d2 < best:
            best = d2
    return math.sqrt(best) if best is not None else 1e30


def _to_local(cx, cy, rot, x, y):
    dx, dy = x - cx, y - cy
    c, s = math.cos(rot), math.sin(rot)
    return dx * c + dy * s, -dx * s + dy * c


def _to_world(cx, cy, rot, lx, ly):
    c, s = math.cos(rot), math.sin(rot)
    return cx + lx * c - ly * s, cy + lx * s + ly * c


def _test_symmetry(points, center, rotation, tol_ft, mirror_x, n_samples):
    """Reflects sampled boundary points about one of the shape's own
    oriented-bbox axes (mirror_x=True: reflect local X, testing
    left-right symmetry; False: reflect local Y, testing top-bottom
    symmetry) and checks each reflection lands back within tol_ft of
    the ORIGINAL outline. True only if every sample does."""
    cx, cy = center
    for x, y in _sample_boundary_points(points, n_samples):
        lx, ly = _to_local(cx, cy, rotation, x, y)
        if mirror_x:
            lx = -lx
        else:
            ly = -ly
        wx, wy = _to_world(cx, cy, rotation, lx, ly)
        if _edge_distance(points, wx, wy) > tol_ft:
            return False
    return True


def classify_profile(footprint, cfg):
    """Returns one of 'rect', 'i_h', 'channel', 'custom' for a single
    (non-hollow, non-circle) outline, using its fill ratio and
    bilateral symmetry about its own oriented-bbox axes. A footprint
    already flagged .is_rectangle by Footprint itself always returns
    'rect' — the nested-loop pass (_pair_hollow_outlines) is what
    actually distinguishes a hollow tube from a solid rectangle, not
    this function."""
    if footprint.is_rectangle:
        return 'rect'
    obb_area = footprint.width_ft * footprint.length_ft
    fill = footprint.area / obb_area if obb_area > 1e-9 else 0.0
    lo, hi = cfg.PROFILE_FILL_RATIO_RANGE
    if not (lo <= fill <= hi):
        return 'custom'

    tol_ft = cfg.SYMMETRY_TOLERANCE_FRACTION * min(footprint.width_ft, footprint.length_ft)
    n = cfg.SYMMETRY_SAMPLE_POINTS
    sym_x = _test_symmetry(footprint.points, footprint.center, footprint.rotation, tol_ft, True, n)
    sym_y = _test_symmetry(footprint.points, footprint.center, footprint.rotation, tol_ft, False, n)
    if sym_x and sym_y:
        return 'i_h'
    if sym_x or sym_y:
        return 'channel'
    return 'custom'


def _pair_hollow_outlines(footprints, cfg):
    """Groups Footprints into (outer, inner) hollow-tube pairs when one
    sits concentrically inside another with a real wall gap on both
    axes. Returns (pairs, remaining) — remaining are the footprints not
    consumed by a pair, in their original relative order."""
    center_tol_ft = cfg.HOLLOW_CENTER_TOLERANCE_MM * cfg.MM_TO_FT
    min_wall_ft = cfg.HOLLOW_MIN_WALL_MM * cfg.MM_TO_FT
    used = set()
    pairs = []
    order = sorted(range(len(footprints)), key=lambda i: -footprints[i].area)
    for oi in order:
        if oi in used:
            continue
        outer = footprints[oi]
        best_ii, best_area = None, None
        for ii, inner in enumerate(footprints):
            if ii == oi or ii in used or inner.area >= outer.area:
                continue
            if (abs(outer.center[0] - inner.center[0]) > center_tol_ft
                    or abs(outer.center[1] - inner.center[1]) > center_tol_ft):
                continue
            wall_w = (outer.width_ft - inner.width_ft) / 2.0
            wall_l = (outer.length_ft - inner.length_ft) / 2.0
            if wall_w < min_wall_ft or wall_l < min_wall_ft:
                continue
            if best_area is None or inner.area > best_area:
                best_ii, best_area = ii, inner.area
        if best_ii is not None:
            pairs.append((outer, footprints[best_ii]))
            used.add(oi)
            used.add(best_ii)
    remaining = [fp for i, fp in enumerate(footprints) if i not in used]
    return pairs, remaining


def scan_perimeter_layer(doc, cad_import, perim_cat, cfg):
    """Classifies every column candidate on one perimeter layer. Returns
    (shapes, report) where report is a dict of diagnostic counts
    ('circles', 'rect', 'i_h', 'channel', 'hollow', 'custom',
    'blocks_unresolved')."""
    from Autodesk.Revit.DB import Options
    from pp_common.cad_read import collect_shape_entities

    opts = Options()
    opts.IncludeNonVisibleObjects = True
    geom = cad_import.get_Geometry(opts)

    # bound-arc chords ON: steel profile fillets (rounded I/H/channel
    # corners) must close their outline loops -- unlike Auto Pile's
    # pile/footing layers, verified without this flag
    buckets = collect_shape_entities(doc, geom, perim_cat,
                                     include_bound_arc_chords=True)

    shapes = []
    report = {
        'circles': 0, 'rect': 0, 'i_h': 0, 'channel': 0, 'hollow': 0,
        'custom': 0, 'blocks_unresolved': 0,
    }

    for cx, cy, r in buckets['circles']:
        shapes.append(CircleColumnShape((cx, cy), r * 2.0, source="circle"))
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
    footprints = []
    for pts in outlines:
        fp = Footprint(pts, rect_area_ratio=cfg.RECT_AREA_RATIO,
                      rotation_snap_deg=cfg.ROTATION_SNAP_DEG)
        if fp.width_ft < min_side or fp.length_ft > max_side:
            continue
        footprints.append(fp)

    pairs, remaining = _pair_hollow_outlines(footprints, cfg)
    for outer, inner in pairs:
        shapes.append(HollowColumnShape(outer, inner))
        report['hollow'] += 1

    for fp in remaining:
        kind = classify_profile(fp, cfg)
        if kind == 'rect':
            shapes.append(RectColumnShape(fp))
            report['rect'] += 1
        elif kind == 'i_h':
            shapes.append(IHColumnShape(fp))
            report['i_h'] += 1
        elif kind == 'channel':
            shapes.append(ChannelColumnShape(fp))
            report['channel'] += 1
        else:
            shapes.append(CustomColumnShape(fp))
            report['custom'] += 1

    # Block-reference classification (method b from Auto Pile: recurse
    # into the block's own geometry) is deferred until a real drawing
    # with column blocks is available to test against -- reported
    # honestly as unresolved rather than guessed.
    report['blocks_unresolved'] = len(buckets['blocks'])

    return shapes, report


def scan_symbol_layer(doc, cad_import, symbol_cat, cfg, existing_shapes=()):
    """Scans a layer that marks a steel column with a small non-scale
    marker glyph (e.g. the stylized wide-flange "H/I" icon confirmed
    against a real drawing) instead of drawing its footprint to scale --
    common when the real section is defined elsewhere (a schedule), not
    by the plan outline. Every marker is a handful of tiny geometry
    fragments (the glyph's individual strokes); they are clustered by
    proximity (cfg.SYMBOL_CLUSTER_MAX_DISTANCE_MM) into one
    SymbolicSteelColumnShape per real column, using each cluster's
    centroid as the placement point. NOT filtered by
    MIN/MAX_FOOTPRINT_SIDE_MM -- these markers are legitimately much
    smaller than any real column.

    existing_shapes: shapes already found on the real perimeter layer
    (may be empty). A marker whose centroid falls inside one of them, or
    within cfg.SYMBOL_DEDUP_MARGIN_MM of its edge, is skipped -- it is
    decorating a column that already has a real footprint, not marking
    a footprint-less one, and must not produce a duplicate instance.
    Deliberately a small FIXED margin, not scaled to the other shape's
    size -- scaling once suppressed a real marker sitting just outside
    an unrelated, much larger nearby rectangle (confirmed live).

    Returns (shapes, count) -- count is the number of raw marker
    fragments found (for the Apply-time diagnostic report), before
    clustering/dedup."""
    from Autodesk.Revit.DB import Options
    from pp_common.cad_read import collect_shape_entities

    opts = Options()
    opts.IncludeNonVisibleObjects = True
    geom = cad_import.get_Geometry(opts)
    buckets = collect_shape_entities(doc, geom, symbol_cat,
                                     include_bound_arc_chords=True)

    # Each glyph fragment's own centroid AND, for polyline fragments
    # (the flange-plate rectangles), its own drawn rotation -- read
    # directly off the fragment via Footprint rather than inferred from
    # inter-fragment offsets, and confirmed live to be consistent across
    # every fragment of the same marker (so any one of them representing
    # the whole cluster is enough).
    points = []
    point_rotations = []
    for pts in buckets['polylines']:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        points.append(((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0))
        fp = Footprint(pts, rect_area_ratio=cfg.RECT_AREA_RATIO,
                      rotation_snap_deg=cfg.ROTATION_SNAP_DEG)
        # fp.rotation is the flange PLATE's own long axis -- i.e. the
        # section's WIDTH direction, perpendicular to its DEPTH axis.
        # Every other shape's .rotation in this codebase means the
        # depth/length axis (what detect_length_axis calibrates
        # against), so +90 deg converts this one to match -- confirmed
        # live: without this, every placed column came out rotated a
        # consistent 90 deg off from the drawing.
        point_rotations.append(fp.rotation + math.pi / 2.0)
    for a, b in buckets['lines']:
        points.append(((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0))
        point_rotations.append(None)
    for cx, cy, _r in buckets['circles']:
        points.append((cx, cy))
        point_rotations.append(None)
    raw_count = len(points)

    # Greedy nearest-existing-cluster grouping: adequate here because
    # markers of the same column sit far closer to each other (~0.3-0.4m
    # apart, confirmed) than distinct columns do (several metres apart)
    # -- a wide safety margin, not a tight tolerance this could misfire
    # on.
    tol_ft = cfg.SYMBOL_CLUSTER_MAX_DISTANCE_MM * cfg.MM_TO_FT
    clusters = []
    for i, pt in enumerate(points):
        placed = False
        for cluster in clusters:
            ccx = sum(points[j][0] for j in cluster) / len(cluster)
            ccy = sum(points[j][1] for j in cluster) / len(cluster)
            if math.hypot(pt[0] - ccx, pt[1] - ccy) <= tol_ft:
                cluster.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])

    dedup_margin_ft = cfg.SYMBOL_DEDUP_MARGIN_MM * cfg.MM_TO_FT
    shapes = []
    for cluster in clusters:
        cx = sum(points[j][0] for j in cluster) / len(cluster)
        cy = sum(points[j][1] for j in cluster) / len(cluster)
        skip = False
        for existing in existing_shapes:
            try:
                if existing.contains(cx, cy):
                    skip = True
                    break
                if existing.dist_to_boundary(cx, cy) < dedup_margin_ft:
                    skip = True
                    break
            except Exception:
                continue
        if not skip:
            rotation = 0.0
            for j in cluster:
                if point_rotations[j] is not None:
                    rotation = point_rotations[j]
                    break
            shapes.append(SymbolicSteelColumnShape((cx, cy), rotation=rotation))

    return shapes, raw_count


# ---------------------------------------------------------------------------
# Label matching (reuses pp_common.labels — the same Hungarian-
# assignment pipeline proven on Auto Foundation/Auto Pile)
# ---------------------------------------------------------------------------

def default_column_label(shape):
    """Size-derived fallback name, used exactly like Auto Pile's
    default_pile_label: columns never adopt a same-size sibling's label
    (adopt_same_size_label=False below) — a size match doesn't mean two
    columns share a design label, so an unmatched column gets a name
    from its own geometry instead of borrowing an unrelated one."""
    def mm(value_ft):
        return int(round(value_ft / MM_TO_FT))
    if isinstance(shape, CircleColumnShape):
        return u"D{}".format(mm(shape.diameter_ft))
    if isinstance(shape, HollowColumnShape):
        return u"HOLLOW{}x{}".format(mm(shape.width_ft), mm(shape.length_ft))
    if isinstance(shape, SymbolicSteelColumnShape):
        # No real size to derive a name from (see the class docstring) --
        # a coordinate-based name is the only option left.
        return u"SYM{}_{}".format(int(round(shape.center[0])), int(round(shape.center[1])))
    prefix = {'rect': u"RECT", 'i_h': u"IH", 'channel': u"CHAN",
             'custom': u"CUSTOM"}.get(shape.display_name, u"COL")
    return u"{}{}x{}".format(prefix, mm(shape.width_ft), mm(shape.length_ft))


def group_by_size_only(shapes):
    """No-label mode: skip CAD text entirely and group purely by each
    shape's own derived name (default_column_label). A column's shape
    signature alone is still decisive for circle/I-H/channel/hollow
    (reconcile_material treats a missing label the same as an
    unrecognized one); only a plain rectangle actually needs a manual
    material pick in this mode, same as with labels.

    Returns OrderedDict label -> [shape], same shape as
    match_and_group's second return value."""
    for s in shapes:
        s.label = default_column_label(s)
    return pp_labels.group_shapes(shapes)


def match_and_group(shapes, texts, cfg):
    """Matches CAD labels to column shapes and groups by label. Mirrors
    Auto Pile's match_and_group: never adopts a same-size shape's label
    (adopt_same_size_label=False), and unmatched shapes get a size-
    derived name via default_column_label instead of a bare X1/X2.

    Returns (matched_count, OrderedDict label -> [shape])."""
    matched = pp_labels.match_labels(
        shapes, texts,
        label_max_distance_mm=cfg.LABEL_MAX_DISTANCE_MM,
        size_group_round_mm=cfg.SIZE_GROUP_ROUND_MM,
        adopt_same_size_label=False,
        synthetic_label_fn=default_column_label)
    groups = pp_labels.group_shapes(shapes)
    return matched, groups


# ---------------------------------------------------------------------------
# Material reconciliation (PRIMARY label cue + SECONDARY shape cue)
# ---------------------------------------------------------------------------

def classify_label_material(label, cfg):
    """PRIMARY cue: returns 'concrete', 'steel', or None (no pattern
    matched, or both did — ambiguous) from the label text alone."""
    if not label:
        return None
    upper = label.upper()
    is_concrete = any(re.match(p, upper) for p in cfg.CONCRETE_LABEL_PATTERNS)
    is_steel = any(re.match(p, upper) for p in cfg.STEEL_LABEL_PATTERNS)
    if is_concrete and not is_steel:
        return 'concrete'
    if is_steel and not is_concrete:
        return 'steel'
    return None


def reconcile_material(shape, label, cfg):
    """Combines the label-prefix cue (PRIMARY) with the shape-signature
    cue (SECONDARY, shape.material_hint). Returns (verdict, reason)
    where verdict is 'concrete', 'steel', or 'UNCLASSIFIED'. Never
    guesses: a rectangle with an absent/ambiguous label, or a label and
    shape that actively disagree, comes back UNCLASSIFIED — the UI (M5)
    exposes a manual material picker for those."""
    label_material = classify_label_material(label, cfg)
    shape_material = shape.material_hint

    if label_material and shape_material:
        if label_material == shape_material:
            return label_material, "label + shape agree"
        return ("UNCLASSIFIED",
               "label reads {} but shape looks like {}".format(
                   label_material, shape_material))
    if label_material and shape_material is None:
        return label_material, "label prefix (shape is an ambiguous rectangle)"
    if shape_material and not label_material:
        return shape_material, "shape signature (label absent or ambiguous)"
    return "UNCLASSIFIED", "label absent/ambiguous and shape is an ambiguous rectangle"
