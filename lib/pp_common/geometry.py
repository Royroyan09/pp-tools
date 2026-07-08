# -*- coding: utf-8 -*-
"""Shape geometry shared by every auto-modelling tool: a closed CAD
outline (Footprint) with oriented-bounding-box sizing/rotation and
rectangularity, plus chaining of loose line segments into closed loops.

Also: Segment (an OPEN 2D line, added for Auto Beam) with parallel-line
pairing and collinearity testing -- every prior tool (Foundation, Pile,
Column) places a family at a POINT from a closed Footprint; Auto Beam
places one along a CURVE, so it needs this open-line counterpart
instead. Kept here (not a new module) since it shares the same "pure
math, no Revit API, testable offline" contract Footprint already has.

Kept independent of any one tool's config module: RECT_AREA_RATIO and
ROTATION_SNAP_DEG are passed in explicitly (defaulting to
pp_common.config_base when omitted) so a tool can override them without
this module importing that tool's bundle-local af_config/ap_config.
"""
import math
import re

from pp_common import config_base


def label_sort_key(label):
    """Natural order so F2 sorts before F11."""
    m = re.match(r'^([A-Za-z]*)(\d+)$', label)
    if m:
        return (0, m.group(1), int(m.group(2)))
    return (1, label, 0)


def round_dimension_mm(value_mm, snap_mm=None, tolerance_mm=None):
    """Snaps a measured CAD dimension to the nearest clean multiple of
    snap_mm, but ONLY when it is already within tolerance_mm of it --
    tracing/OCR imprecision (1989.99mm meant as 2000mm) gets cleaned up,
    but a dimension that far from any clean step is left exactly as
    measured rather than forced to one (never guess a genuinely custom
    size). Defaults to config_base.DIMENSION_SNAP_MM/
    DIMENSION_SNAP_TOLERANCE_MM when omitted.

    Returns (value_mm, was_rounded)."""
    if snap_mm is None:
        snap_mm = config_base.DIMENSION_SNAP_MM
    if tolerance_mm is None:
        tolerance_mm = config_base.DIMENSION_SNAP_TOLERANCE_MM
    if not snap_mm or tolerance_mm <= 0:
        return value_mm, False
    nearest = round(value_mm / snap_mm) * snap_mm
    if abs(value_mm - nearest) <= tolerance_mm:
        return nearest, abs(nearest - value_mm) > 1e-6
    return value_mm, False


def norm_half_pi(angle):
    """Normalizes an angle to (-90, 90] degrees; rectangles are symmetric
    under 180-degree rotation so this is the effective rotation range."""
    while angle > math.pi / 2.0:
        angle -= math.pi
    while angle <= -math.pi / 2.0:
        angle += math.pi
    return angle


class Footprint(object):
    """One closed CAD outline in Revit model coordinates (feet)."""

    def __init__(self, points, rect_area_ratio=None, rotation_snap_deg=None):
        """points: [(x, y)] in feet, model coordinates, ordered along the
        outline, no repeated closing point (closure is implicit)."""
        if rect_area_ratio is None:
            rect_area_ratio = config_base.RECT_AREA_RATIO
        if rotation_snap_deg is None:
            rotation_snap_deg = config_base.ROTATION_SNAP_DEG

        self.points = points

        # polygon area + centroid (shoelace); centroid is used for label
        # distance so odd shapes (L, triangle) match their nearest text
        a2 = 0.0
        cx = 0.0
        cy = 0.0
        n = len(points)
        for i in range(n):
            x0, y0 = points[i]
            x1, y1 = points[(i + 1) % n]
            cross = x0 * y1 - x1 * y0
            a2 += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
        self.area = abs(a2) / 2.0
        if abs(a2) > 1e-9:
            self.centroid = (cx / (3.0 * a2), cy / (3.0 * a2))
        else:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            self.centroid = ((min(xs) + max(xs)) / 2.0,
                             (min(ys) + max(ys)) / 2.0)

        self._compute_obb(rotation_snap_deg)

        # a footprint counts as rectangular when it fills (almost) all of
        # its oriented bounding box; triangles come out around 0.5
        obb_area = self.width_ft * self.length_ft
        self.is_rectangle = (obb_area > 1e-9 and
                             self.area / obb_area >= rect_area_ratio)
        self.label = None

    def _compute_obb(self, rotation_snap_deg):
        """Minimum-area oriented bounding box (rotating calipers over the
        outline's edge directions). Sets center, width_ft, length_ft and
        rotation (angle of the long axis vs. X, radians)."""
        pts = self.points
        best = None  # (area, ux, uy, umin, umax, vmin, vmax)
        n = len(pts)
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            dx, dy = x1 - x0, y1 - y0
            edge_len = math.sqrt(dx * dx + dy * dy)
            if edge_len < 1e-9:
                continue
            ux, uy = dx / edge_len, dy / edge_len
            us = [p[0] * ux + p[1] * uy for p in pts]
            vs = [-p[0] * uy + p[1] * ux for p in pts]
            umin, umax = min(us), max(us)
            vmin, vmax = min(vs), max(vs)
            area = (umax - umin) * (vmax - vmin)
            if best is None or area < best[0]:
                best = (area, ux, uy, umin, umax, vmin, vmax)

        if best is None:
            # degenerate outline; fall back to the axis-aligned box
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            dx, dy = max(xs) - min(xs), max(ys) - min(ys)
            self.center = self.centroid
            self.width_ft, self.length_ft = min(dx, dy), max(dx, dy)
            self.rotation = math.pi / 2.0 if dy > dx else 0.0
            return

        _area, ux, uy, umin, umax, vmin, vmax = best
        du, dv = umax - umin, vmax - vmin
        cu, cv = (umin + umax) / 2.0, (vmin + vmax) / 2.0
        # back-transform the OBB center to model coordinates
        self.center = (cu * ux - cv * uy, cu * uy + cv * ux)
        self.width_ft = min(du, dv)
        self.length_ft = max(du, dv)

        angle = math.atan2(uy, ux)
        if dv > du:
            angle += math.pi / 2.0
        angle = norm_half_pi(angle)
        snap = math.radians(rotation_snap_deg)
        for target in (0.0, math.pi / 2.0, -math.pi / 2.0):
            if abs(angle - target) <= snap:
                angle = target
                break
        if angle == -math.pi / 2.0:
            angle = math.pi / 2.0
        self.rotation = angle

    def contains(self, x, y):
        """Ray-casting point-in-polygon (Z ignored)."""
        inside = False
        pts = self.points
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > y) != (yj > y):
                x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
                if x < x_cross:
                    inside = not inside
            j = i
        return inside

    def dist2_to_center(self, x, y):
        cx, cy = self.centroid
        return (x - cx) ** 2 + (y - cy) ** 2

    def dist_to_boundary(self, x, y):
        """Distance from (x, y) to the outline: 0 inside, otherwise the
        nearest point on any edge. Leader-style labels sit just outside
        their shape, so edge distance matches far better than centroid
        distance (a large shape's centroid can be farther from its own
        label than a small neighbour's)."""
        if self.contains(x, y):
            return 0.0
        best = None
        pts = self.points
        n = len(pts)
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            vx, vy = x1 - x0, y1 - y0
            seg2 = vx * vx + vy * vy
            if seg2 < 1e-12:
                d2 = (x - x0) ** 2 + (y - y0) ** 2
            else:
                t = ((x - x0) * vx + (y - y0) * vy) / seg2
                t = max(0.0, min(1.0, t))
                dx = x0 + t * vx - x
                dy = y0 + t * vy - y
                d2 = dx * dx + dy * dy
            if best is None or d2 < best:
                best = d2
        return math.sqrt(best) if best is not None else 1e30


def chain_lines_to_loops(segments, tol_ft):
    """Chains loose ((x,y),(x,y)) segments into closed loops (endpoint
    tolerance tol_ft). Returns a list of point lists without the
    repeated closing point. Open chains are dropped."""

    def near(a, b):
        return abs(a[0] - b[0]) <= tol_ft and abs(a[1] - b[1]) <= tol_ft

    segs = list(segments)
    loops = []
    while segs:
        p0, p1 = segs.pop(0)
        chain = [p0, p1]
        extended = True
        while extended and not near(chain[0], chain[-1]):
            extended = False
            for i in range(len(segs)):
                a, b = segs[i]
                if near(a, chain[-1]):
                    chain.append(b)
                elif near(b, chain[-1]):
                    chain.append(a)
                elif near(a, chain[0]):
                    chain.insert(0, b)
                elif near(b, chain[0]):
                    chain.insert(0, a)
                else:
                    continue
                segs.pop(i)
                extended = True
                break
        if len(chain) >= 4 and near(chain[0], chain[-1]):
            loops.append(chain[:-1])
    return loops


# ---------------------------------------------------------------------------
# Open-line geometry (Auto Beam): centerlines instead of closed Footprints
# ---------------------------------------------------------------------------

class Segment(object):
    """One open 2D line segment in Revit model coordinates (feet) --
    the line/curve counterpart to Footprint's closed outlines. A beam's
    centerline (or one of its two parallel edge lines, before pairing)
    is a Segment, not a Footprint: there is no interior to test a point
    against, only a direction and a length."""

    def __init__(self, p0, p1):
        self.p0 = p0
        self.p1 = p1
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        self.length = math.hypot(dx, dy)
        # a line has no direction (p0->p1 and p1->p0 are the same line),
        # so angle is normalized into [0, pi) rather than the full circle
        self.angle = math.atan2(dy, dx) % math.pi if self.length > 1e-9 else 0.0
        self.midpoint = ((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0)

    def unit_vector(self):
        """Direction from p0 to p1 (NOT normalized like .angle -- this
        one does distinguish the two endpoints, for callers that walk
        along the segment)."""
        if self.length < 1e-9:
            return (0.0, 0.0)
        dx, dy = self.p1[0] - self.p0[0], self.p1[1] - self.p0[1]
        return (dx / self.length, dy / self.length)

    def project(self, pt):
        """Decomposes pt relative to this segment's own infinite line:
        returns (along, perp) where along is the signed distance from
        p0 along the segment's direction (0..length spans the segment
        itself; outside that range means pt's projection falls beyond
        an endpoint) and perp is the signed perpendicular distance from
        the line (0 = exactly on it)."""
        ux, uy = self.unit_vector()
        vx, vy = pt[0] - self.p0[0], pt[1] - self.p0[1]
        along = vx * ux + vy * uy
        perp = vx * (-uy) + vy * ux
        return along, perp


def _angle_diff(a1, a2):
    """Smallest difference between two [0, pi) line angles, itself in
    [0, pi/2] (a line's angle wraps at pi, so e.g. angles 0.01 and
    pi-0.01 are almost parallel, not near-perpendicular)."""
    d = abs(a1 - a2) % math.pi
    return min(d, math.pi - d)


def is_collinear(seg_a, seg_b, angle_tol_deg, offset_tol_ft):
    """True when seg_a and seg_b lie on (close enough to) the same
    infinite line -- same direction within angle_tol_deg AND seg_b's
    midpoint falls within offset_tol_ft of seg_a's own line. Does NOT
    check whether they're close enough end-to-end to actually stitch
    (that's a separate gap-distance decision the caller makes, e.g.
    Auto Beam's KOLOM-bridging rule) -- this only answers "same line or
    not", which is also true for two segments that merely cross it at
    a T-junction if their angles differ, correctly excluding those."""
    if _angle_diff(seg_a.angle, seg_b.angle) > math.radians(angle_tol_deg):
        return False
    _along, perp = seg_a.project(seg_b.midpoint)
    return abs(perp) <= offset_tol_ft


def pair_parallel_lines(segments, max_gap_ft, parallel_tol_deg, min_overlap_ft,
                        min_gap_ft=None):
    """Pairs up segments that are roughly parallel (within
    parallel_tol_deg), within max_gap_ft of each other perpendicular to
    their shared direction, and overlap along that direction by at
    least min_overlap_ft -- Auto Beam's 2-line mode, where a beam is
    drawn as two parallel edge lines rather than one centerline.

    min_gap_ft rejects near-zero gaps (default: config_base.
    COLLINEAR_OFFSET_TOL_MM converted to feet -- the same tolerance used
    to call two segments "the same line" elsewhere, so a gap below it
    means these aren't two edges at all). Without this, two collinear
    pieces of a SINGLE edge (e.g. a beam edge split across a polyline
    vertex, or two abutting line entities) satisfy every other check
    and get "paired" with each other into a nonsensical 0mm-wide beam --
    confirmed against a real drawing where this happened.

    Every candidate pair passing all checks is scored by gap (smaller
    is better); pairs are assigned greedily smallest-gap-first, each
    segment used in at most one pair -- adequate here (not a full
    optimal assignment like pp_common.labels' Hungarian matcher) because
    a real beam's two edges are much closer to each other than to any
    other line in the drawing, so ties/near-ties between competing
    pairings are not expected in practice.

    Returns (pairs, unpaired) where pairs is [(centerline_segment,
    width_ft, seg_a, seg_b), ...] and unpaired is the list of segments
    left over (too far from any partner, not parallel to one, not
    overlapping one, or only "paired" at a near-zero gap) -- the caller
    decides what to do with those (e.g. fall back to treating them as
    1-line centerlines)."""
    if min_gap_ft is None:
        min_gap_ft = config_base.COLLINEAR_OFFSET_TOL_MM * config_base.MM_TO_FT
    angle_tol = math.radians(parallel_tol_deg)
    n = len(segments)
    candidates = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = segments[i], segments[j]
            if a.length < 1e-9 or b.length < 1e-9:
                continue
            if _angle_diff(a.angle, b.angle) > angle_tol:
                continue
            _along, gap = a.project(b.midpoint)
            gap = abs(gap)
            if gap < min_gap_ft or gap > max_gap_ft:
                continue
            # overlap along a's own direction: project both of b's
            # endpoints onto a's line and intersect with a's own span
            a_along0, _ = a.project(a.p0)
            a_along1, _ = a.project(a.p1)
            a_lo, a_hi = min(a_along0, a_along1), max(a_along0, a_along1)
            b_along0, _ = a.project(b.p0)
            b_along1, _ = a.project(b.p1)
            b_lo, b_hi = min(b_along0, b_along1), max(b_along0, b_along1)
            overlap = min(a_hi, b_hi) - max(a_lo, b_lo)
            if overlap < min_overlap_ft:
                continue
            candidates.append((gap, i, j))

    candidates.sort(key=lambda c: c[0])
    used = set()
    pairs = []
    for gap, i, j in candidates:
        if i in used or j in used:
            continue
        used.add(i)
        used.add(j)
        a, b = segments[i], segments[j]
        # centerline: midpoint of each endpoint pair closest to each
        # other, i.e. average a and b's own midpoints projected onto
        # the shared direction -- simplest robust choice when the two
        # edges aren't perfectly equal length (a beam drawn slightly
        # unevenly at its ends)
        cx = (a.midpoint[0] + b.midpoint[0]) / 2.0
        cy = (a.midpoint[1] + b.midpoint[1]) / 2.0
        ux, uy = a.unit_vector()
        half = max(a.length, b.length) / 2.0
        centerline = Segment((cx - ux * half, cy - uy * half),
                             (cx + ux * half, cy + uy * half))
        pairs.append((centerline, gap, a, b))

    unpaired = [segments[k] for k in range(n) if k not in used]
    return pairs, unpaired
