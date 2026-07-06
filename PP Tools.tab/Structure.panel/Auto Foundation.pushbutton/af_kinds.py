# -*- coding: utf-8 -*-
"""Foundation kinds for Auto Foundation.

FoundationKind is the extension point for foundation categories:

  * IsolatedFooting  — rectangular pad (Footing-Rectangular family),
                       auto-rotated to match the CAD outline
  * CustomPadFooting — triangular / L-shaped / arbitrary pad, generated
                       as a structural foundation slab from the exact
                       CAD outline
  * StripFooting, PileCap, RaftFoundation — stubs for later versions

Footprint interpretation (oriented bounding box, rectangularity test,
labelling) is shared; detection rules and tolerances live in af_config.
"""
import math
import os
import re
from collections import OrderedDict

import clr
clr.AddReference('RevitAPI')

from System.Collections.Generic import List as NetList

from Autodesk.Revit.DB import (
    FilteredElementCollector, FamilySymbol, BuiltInCategory,
    BuiltInParameter, ElementTransformUtils, Element, Line, StorageType,
    XYZ, Floor, FloorType, CurveLoop
)
from Autodesk.Revit.DB.Structure import StructuralType

import af_config as cfg


def _name(element):
    if element is None:
        return ""
    try:
        return Element.Name.__get__(element)
    except Exception:
        try:
            return element.Name
        except Exception:
            return ""


def _label_sort_key(label):
    # natural order so F2 sorts before F11
    m = re.match(r'^([A-Za-z]*)(\d+)$', label)
    if m:
        return (0, m.group(1), int(m.group(2)))
    return (1, label, 0)


def _norm_half_pi(angle):
    """Normalizes an angle to (-90, 90] degrees; rectangles are symmetric
    under 180-degree rotation so this is the effective rotation range."""
    while angle > math.pi / 2.0:
        angle -= math.pi
    while angle <= -math.pi / 2.0:
        angle += math.pi
    return angle


def _hungarian(cost):
    """Minimum-total-cost assignment (Hungarian algorithm, O(n^2*m)).
    cost: rectangular matrix with len(cost) <= len(cost[0]). Returns a
    list mapping each row to its assigned column."""
    inf = float('inf')
    n = len(cost)
    m = len(cost[0])
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = -1
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0 != 0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    result = [-1] * n
    for j in range(1, m + 1):
        if p[j] != 0:
            result[p[j] - 1] = j - 1
    return result


# ---------------------------------------------------------------------------
# Footprint: one closed CAD outline in Revit model coordinates (feet)
# ---------------------------------------------------------------------------

class Footprint(object):

    def __init__(self, points):
        """points: [(x, y)] in feet, model coordinates, ordered along the
        outline, no repeated closing point (closure is implicit)."""
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

        self._compute_obb()

        # a footprint counts as rectangular when it fills (almost) all of
        # its oriented bounding box; triangles come out around 0.5
        obb_area = self.width_ft * self.length_ft
        self.is_rectangle = (obb_area > 1e-9 and
                             self.area / obb_area >= cfg.RECT_AREA_RATIO)
        self.label = None

    def _compute_obb(self):
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
        # normalize the long-axis angle to (-90, 90] and snap tiny noise
        # to the nearest axis so clean orthogonal footings don't rotate
        while angle > math.pi / 2.0:
            angle -= math.pi
        while angle <= -math.pi / 2.0:
            angle += math.pi
        snap = math.radians(cfg.ROTATION_SNAP_DEG)
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
        their footing, so edge distance matches far better than centroid
        distance (a large footing's centroid can be farther from its own
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


def _curve_loop(points, z):
    """Builds a closed horizontal CurveLoop at elevation z from ordered
    2D points, skipping degenerate segments."""
    pts = []
    for p in points:
        if pts and abs(p[0] - pts[-1][0]) < 1e-6 and abs(p[1] - pts[-1][1]) < 1e-6:
            continue
        pts.append(p)
    if len(pts) >= 2 and (abs(pts[0][0] - pts[-1][0]) < 1e-6
                          and abs(pts[0][1] - pts[-1][1]) < 1e-6):
        pts.pop()
    if len(pts) < 3:
        raise ValueError("outline has fewer than 3 usable points")
    loop = CurveLoop()
    n = len(pts)
    for i in range(n):
        a = pts[i]
        b = pts[(i + 1) % n]
        if math.sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) < 1e-6:
            continue
        loop.Append(Line.CreateBound(XYZ(a[0], a[1], z), XYZ(b[0], b[1], z)))
    return loop


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------

class FoundationKind(object):
    """Base class for foundation categories. Subclasses implement
    generate(); footprint labelling/grouping is shared."""

    display_name = "foundation"

    def _size_key(self, fp):
        round_ft = cfg.SIZE_GROUP_ROUND_MM * cfg.MM_TO_FT
        return (int(round(fp.width_ft / round_ft)),
                int(round(fp.length_ft / round_ft)))

    def match_labels(self, footprints, texts):
        """texts: [(x, y, value)] already cleaned/filtered. Four passes:

        1. a text whose insertion point falls INSIDE a perimeter labels it
        2. remaining texts/footprints are paired globally nearest-first by
           edge distance, one text per footprint — labels with leaders sit
           outside their footing, and per-footprint nearest matching lets
           a neighbour's label steal, so pairing must be one-to-one
        3. still-unlabelled footprints adopt the label of an already
           labelled footprint with the same (rounded) size
        4. whatever is left gets synthetic labels, one per size

        Returns the number of footprints labelled from CAD text (1+2)."""
        matched = 0
        used = set()

        # pass 1: insertion point inside the perimeter
        for fp in footprints:
            inside = [(i, t) for i, t in enumerate(texts)
                      if i not in used and fp.contains(t[0], t[1])]
            if inside:
                i, t = min(inside,
                           key=lambda it: fp.dist2_to_center(it[1][0], it[1][1]))
                fp.label = t[2]
                used.add(i)
                matched += 1

        # pass 2: optimal one-to-one pairing by edge distance (minimum
        # total cost, Hungarian). Nearest-first greedy is NOT enough: a
        # small footing next to a big one's leader label steals it even
        # though the globally cheapest pairing gives both their own text.
        max_d = cfg.LABEL_MAX_DISTANCE_MM
        max_d_ft = max_d * cfg.MM_TO_FT if max_d else None
        big = 1e9
        free_f = [i for i, fp in enumerate(footprints) if fp.label is None]
        free_t = [i for i in range(len(texts)) if i not in used]
        if free_f and free_t:
            cost = []
            for fi in free_f:
                row = []
                for ti in free_t:
                    d = footprints[fi].dist_to_boundary(texts[ti][0], texts[ti][1])
                    if max_d_ft is not None and d > max_d_ft:
                        d = big
                    row.append(d)
                cost.append(row)
            # the solver needs rows <= columns; transpose when there are
            # more footprints than texts
            transposed = len(cost) > len(cost[0])
            if transposed:
                cost = [[cost[r][c] for r in range(len(cost))]
                        for c in range(len(cost[0]))]
            assignment = _hungarian(cost)
            for r, c in enumerate(assignment):
                if c < 0 or cost[r][c] >= big:
                    continue
                fi = free_f[c] if transposed else free_f[r]
                ti = free_t[r] if transposed else free_t[c]
                footprints[fi].label = texts[ti][2]
                used.add(ti)
                matched += 1

        # pass 3: adopt the label of a same-size labelled footprint
        # (drawings often label only one representative footing per type)
        size_to_label = {}
        for fp in footprints:
            if fp.label is not None:
                size_to_label.setdefault(self._size_key(fp), fp.label)
        for fp in footprints:
            if fp.label is None:
                fp.label = size_to_label.get(self._size_key(fp))

        # pass 4: synthetic labels for whatever is still unmatched
        size_labels = {}
        for fp in footprints:
            if fp.label is not None:
                continue
            key = self._size_key(fp)
            if key not in size_labels:
                size_labels[key] = cfg.UNMATCHED_LABEL_FORMAT.format(
                    n=len(size_labels) + 1)
            fp.label = size_labels[key]
        return matched

    def group_footprints(self, footprints):
        """OrderedDict label -> [Footprint], labels naturally sorted."""
        groups = {}
        for fp in footprints:
            groups.setdefault(fp.label, []).append(fp)
        ordered = OrderedDict()
        for label in sorted(groups.keys(), key=_label_sort_key):
            ordered[label] = groups[label]
        return ordered

    def generate(self, doc, level, groups):
        """Creates types + instances. Must be called inside an already
        open Transaction. groups: [{'label', 'width_ft', 'length_ft',
        'thickness_ft', 'footprints'}]. Returns (created_count,
        warnings)."""
        raise NotImplementedError(
            "{} generation is not implemented yet.".format(self.display_name))

    # -- shared placement helper ------------------------------------------

    def _flush_top_with_level(self, doc, instances, elev, offset_bip):
        """The family/type origin behaviour is not assumed: measures each
        placed element's top face and corrects its level offset so the TOP
        sits flush with the level, thickness extending down. Returns a
        list of warnings."""
        warnings = []
        doc.Regenerate()
        for inst in instances:
            try:
                bb = inst.get_BoundingBox(None)
                if bb is None:
                    continue
                dz = elev - bb.Max.Z
                if abs(dz) > cfg.TOP_FLUSH_TOLERANCE_FT:
                    off = inst.get_Parameter(offset_bip)
                    if off is not None and not off.IsReadOnly:
                        off.Set(off.AsDouble() + dz)
            except Exception as ex:
                warnings.append(u"Top-flush check failed: {}".format(ex))
        return warnings


class IsolatedFooting(FoundationKind):
    """Rectangular isolated pad footing, auto-rotated to the CAD outline."""

    display_name = "isolated pad footing"

    # -- family / parameter resolution -----------------------------------

    def find_base_symbol(self, doc):
        collector = (FilteredElementCollector(doc)
                     .OfCategory(BuiltInCategory.OST_StructuralFoundation)
                     .OfClass(FamilySymbol))
        wanted = [c.lower() for c in cfg.FAMILY_NAME_CANDIDATES]
        for sym in collector:
            try:
                if sym.Family.Name.lower() in wanted:
                    return sym
            except Exception:
                continue
        return None

    def load_family(self, doc):
        """Loads the default rectangular footing family from the library
        roots. Must be called inside a transaction. Returns the loaded
        file path or None."""
        for root in cfg.FAMILY_LIBRARY_ROOTS:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for cand in cfg.FAMILY_FILE_CANDIDATES:
                    if cand in filenames:
                        path = os.path.join(dirpath, cand)
                        try:
                            if doc.LoadFamily(path):
                                return path
                        except Exception:
                            pass
        return None

    def resolve_params(self, symbol):
        """Finds the actual Width / Length / Thickness type parameter
        names on this symbol (they vary between family versions).
        Returns {'width': name-or-None, 'length': ..., 'thickness': ...}."""
        found = {'width': None, 'length': None, 'thickness': None}
        candidates = (
            ('width', [c.lower() for c in cfg.WIDTH_PARAM_CANDIDATES]),
            ('length', [c.lower() for c in cfg.LENGTH_PARAM_CANDIDATES]),
            ('thickness', [c.lower() for c in cfg.THICKNESS_PARAM_CANDIDATES]),
        )
        for p in symbol.Parameters:
            try:
                if p.StorageType != StorageType.Double or p.IsReadOnly:
                    continue
                name = p.Definition.Name.strip()
            except Exception:
                continue
            low = name.lower()
            for key, cands in candidates:
                if found[key] is None and low in cands:
                    found[key] = name
        return found

    # -- generation -------------------------------------------------------

    def _detect_length_axis(self, doc, inst, width_ft, length_ft):
        """Measures a freshly placed, unrotated instance to find which
        model axis the family's Length parameter runs along. Returns 0.0
        (along X), pi/2 (along Y), or None when it cannot be determined
        (square footing, no bounding box)."""
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

    def generate(self, doc, level, groups):
        base = self.find_base_symbol(doc)
        if base is None:
            if self.load_family(doc):
                doc.Regenerate()
                base = self.find_base_symbol(doc)
        if base is None:
            raise ValueError(
                "No rectangular footing family is loaded and none was found "
                "in the Revit library. Load '{}' into the project and try "
                "again.".format(cfg.FAMILY_NAME_CANDIDATES[0]))

        params = self.resolve_params(base)
        missing = [k for k in ('width', 'length', 'thickness')
                   if params.get(k) is None]
        if missing:
            raise ValueError(
                "Family '{}' has no writable {} type parameter(s). Adjust "
                "the candidates in af_config.py.".format(
                    base.Family.Name, ", ".join(missing)))

        family = base.Family
        existing = {}
        for sid in family.GetFamilySymbolIds():
            sym = doc.GetElement(sid)
            existing[_name(sym)] = sym

        created = 0
        warnings = []
        elev = level.ProjectElevation
        # Which model axis the family's Length parameter runs along is NOT
        # assumed (it differs between family versions): it is measured from
        # the first placed non-square instance's bounding box before any
        # rotation, and every rotation is taken relative to that axis.
        length_axis = None

        for g in groups:
            type_name = cfg.TYPE_NAME_FORMAT.format(label=g['label'])
            try:
                sym = existing.get(type_name)
                if sym is None:
                    sym = base.Duplicate(type_name)
                    existing[type_name] = sym
                sym.LookupParameter(params['width']).Set(g['width_ft'])
                sym.LookupParameter(params['length']).Set(g['length_ft'])
                sym.LookupParameter(params['thickness']).Set(g['thickness_ft'])
                if not sym.IsActive:
                    sym.Activate()
                doc.Regenerate()
            except Exception as ex:
                warnings.append(u"Type '{}': {}".format(type_name, ex))
                continue

            instances = []
            for fp in g['footprints']:
                try:
                    pt = XYZ(fp.center[0], fp.center[1], elev)
                    inst = doc.Create.NewFamilyInstance(
                        pt, sym, level, StructuralType.Footing)
                    if length_axis is None:
                        length_axis = self._detect_length_axis(
                            doc, inst, g['width_ft'], g['length_ft'])
                    delta = _norm_half_pi(fp.rotation - (length_axis or 0.0))
                    if abs(delta) > 1e-6:
                        axis = Line.CreateBound(pt, XYZ(pt.X, pt.Y, pt.Z + 1.0))
                        ElementTransformUtils.RotateElement(
                            doc, inst.Id, axis, delta)
                    instances.append(inst)
                    created += 1
                except Exception as ex:
                    warnings.append(u"'{}' at ({:.2f}, {:.2f}): {}".format(
                        type_name, fp.center[0], fp.center[1], ex))

            warnings.extend(self._flush_top_with_level(
                doc, instances, elev,
                BuiltInParameter.INSTANCE_FREE_HOST_OFFSET_PARAM))

        return created, warnings


class CustomPadFooting(FoundationKind):
    """Non-rectangular pad (triangle, L-shape, any closed outline).

    Generated as a structural foundation slab (a Floor whose type has
    IsFoundationSlab set) built from the exact CAD outline, one type per
    label with the user's thickness."""

    display_name = "custom pad footing"

    def find_base_type(self, doc):
        wanted = [c.lower() for c in cfg.FOUNDATION_SLAB_TYPE_CANDIDATES]
        fallback = None
        for ft in FilteredElementCollector(doc).OfClass(FloorType):
            try:
                if not ft.IsFoundationSlab:
                    continue
            except Exception:
                continue
            nm = _name(ft).lower()
            for w in wanted:
                if w in nm:
                    return ft
            if fallback is None:
                fallback = ft
        return fallback

    def _set_thickness(self, floor_type, thickness_ft):
        cs = floor_type.GetCompoundStructure()
        if cs is None:
            raise ValueError("type has no compound structure")
        idx = cs.StructuralMaterialIndex
        if idx < 0 or idx >= cs.LayerCount:
            widths = [cs.GetLayerWidth(i) for i in range(cs.LayerCount)]
            idx = widths.index(max(widths))
        cs.SetLayerWidth(idx, thickness_ft)
        floor_type.SetCompoundStructure(cs)

    def generate(self, doc, level, groups):
        base = self.find_base_type(doc)
        if base is None:
            raise ValueError(
                "No structural foundation slab type was found in this "
                "project. Create one (a slab type with the 'Foundation "
                "Slab' function, e.g. duplicate a floor type in Structure "
                "> Foundation > Slab) and generate again.")

        existing = {}
        for ft in FilteredElementCollector(doc).OfClass(FloorType):
            try:
                if ft.IsFoundationSlab:
                    existing[_name(ft)] = ft
            except Exception:
                continue

        created = 0
        warnings = []
        elev = level.ProjectElevation

        for g in groups:
            type_name = cfg.TYPE_NAME_FORMAT.format(label=g['label'])
            try:
                ft = existing.get(type_name)
                if ft is None:
                    ft = base.Duplicate(type_name)
                    existing[type_name] = ft
                self._set_thickness(ft, g['thickness_ft'])
            except Exception as ex:
                warnings.append(u"Type '{}': {}".format(type_name, ex))
                continue

            floors = []
            for fp in g['footprints']:
                try:
                    loops = NetList[CurveLoop]()
                    loops.Add(_curve_loop(fp.points, elev))
                    fl = Floor.Create(doc, loops, ft.Id, level.Id)
                    floors.append(fl)
                    created += 1
                except Exception as ex:
                    warnings.append(u"'{}' at ({:.2f}, {:.2f}): {}".format(
                        type_name, fp.centroid[0], fp.centroid[1], ex))

            warnings.extend(self._flush_top_with_level(
                doc, floors, elev,
                BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM))

        return created, warnings


class StripFooting(FoundationKind):
    """Continuous (strip) footing under walls — NOT implemented yet.

    Planned: trace centerline pairs / polylines on the perimeter layer,
    derive width from the offset distance and sweep a wall foundation or
    structural framing along the path."""
    display_name = "strip footing"


class PileCap(FoundationKind):
    """Pile cap with pile layout — NOT implemented yet.

    Planned: read cap outline + pile circles on a pile layer, place a
    pile-cap family with the matching pile count/arrangement."""
    display_name = "pile cap"


class RaftFoundation(FoundationKind):
    """Raft / mat slab — NOT implemented yet.

    Planned: build a floor-based foundation slab from the outline with
    openings, using SlabFoundation / floor creation APIs."""
    display_name = "raft foundation"
