# -*- coding: utf-8 -*-
"""Beam centerline detection for Auto Beam.

Every prior tool (Foundation, Pile, Column) reads CLOSED outlines from
the CAD and places a family at each one's center point. A beam has no
interior to close a loop around -- it is drawn as either:

  * 2-LINE mode (usual): two parallel edge lines, gap apart = width b.
    pp_common.geometry.pair_parallel_lines() pairs them; the pair's
    midline becomes the beam's CENTERLINE.
  * 1-LINE mode: the single drawn line IS the centerline already; width
    b has no CAD source in this mode and comes from the label/table
    later (M4/M5), not from geometry here.

scan_beam_layer() is the M2 entry point: collects every raw line/
polyline-edge segment on one CAD layer, filters obvious noise by
length, and dispatches to pairing (2-line) or passes segments through
as-is (1-line). Continuity stitching across gaps (M3) and label/
material matching (M4) are layered on top of what this returns, not
folded into it.
"""
import math
import re

from pp_common.geometry import Segment, pair_parallel_lines, is_collinear, Footprint, chain_lines_to_loops
from pp_common import labels as pp_labels
from pp_common.config_base import MM_TO_FT


def _collect_raw_segments(doc, cad_import, layer_cat, min_length_ft):
    """Walks every line and polyline on one CAD layer (layer_cat may be
    a single Category or a pp_common.wpf_helpers.LayerSelection
    spanning several real layers -- collect_shape_entities normalizes
    either) and returns a flat list of Segment objects (a polyline's
    own vertices are split into consecutive-pair segments -- a beam
    edge is occasionally drawn as a multi-vertex polyline rather than
    a single line). Segments shorter than min_length_ft are dropped as
    noise (dimension ticks, hatching, text strokes) -- mirrors
    MIN_FOOTPRINT_SIDE_MM's role for the point-placed tools, just with
    no matching upper bound: a real beam centerline can legitimately
    run the length of the building."""
    from Autodesk.Revit.DB import Options
    from pp_common.cad_read import collect_shape_entities

    opts = Options()
    opts.IncludeNonVisibleObjects = True
    geom = cad_import.get_Geometry(opts)
    buckets = collect_shape_entities(doc, geom, layer_cat,
                                     include_bound_arc_chords=False)

    segments = []
    for a, b in buckets['lines']:
        seg = Segment(a, b)
        if seg.length >= min_length_ft:
            segments.append(seg)
    for pts in buckets['polylines']:
        for i in range(len(pts) - 1):
            seg = Segment(pts[i], pts[i + 1])
            if seg.length >= min_length_ft:
                segments.append(seg)

    report = {
        'raw_lines': len(buckets['lines']),
        'raw_polylines': len(buckets['polylines']),
        'segments_after_filter': len(segments),
        'circles_ignored': len(buckets['circles']),
        'blocks_ignored': len(buckets['blocks']),
    }
    return segments, report


def scan_beam_layer(doc, cad_import, layer_cat, mode, cfg):
    """mode: '2-line' or '1-line'. Returns (centerlines, report) where
    centerlines is [{'segment': Segment, 'width_ft': float-or-None,
    'edge_a': Segment-or-None, 'edge_b': Segment-or-None}], one dict per
    detected beam centerline (paired or single depending on mode), and
    report is a dict of diagnostic counts for the Apply-time summary."""
    min_len_ft = cfg.MIN_SEGMENT_LENGTH_MM * cfg.MM_TO_FT
    segments, report = _collect_raw_segments(doc, cad_import, layer_cat, min_len_ft)

    centerlines = []
    if mode == '2-line':
        max_gap_ft = cfg.LINE_PAIR_MAX_GAP_MM * cfg.MM_TO_FT
        min_overlap_ft = cfg.LINE_PAIR_MIN_OVERLAP_MM * cfg.MM_TO_FT
        pairs, unpaired = pair_parallel_lines(
            segments, max_gap_ft=max_gap_ft,
            parallel_tol_deg=cfg.LINE_PARALLEL_TOL_DEG,
            min_overlap_ft=min_overlap_ft)
        for centerline, width_ft, edge_a, edge_b in pairs:
            centerlines.append({
                'segment': centerline, 'width_ft': width_ft,
                'edge_a': edge_a, 'edge_b': edge_b,
            })
        report['paired'] = len(pairs)
        report['unpaired'] = len(unpaired)
    else:
        for seg in segments:
            centerlines.append({
                'segment': seg, 'width_ft': None,
                'edge_a': None, 'edge_b': None,
            })
        report['paired'] = 0
        report['unpaired'] = 0

    return centerlines, report


# ---------------------------------------------------------------------------
# Column footprints (continuity through columns) -- a lightweight scan,
# NOT full column classification (that stays Auto Column's job): only
# .contains(x, y) and .center are needed here, to test whether a
# stitching gap is spanned by a real column.
# ---------------------------------------------------------------------------

class _CircleFootprint(object):
    display_name = "circle"

    def __init__(self, center, radius_ft):
        self.center = center
        self.radius_ft = radius_ft

    def contains(self, x, y):
        dx, dy = x - self.center[0], y - self.center[1]
        return (dx * dx + dy * dy) <= self.radius_ft * self.radius_ft


def scan_column_footprints(doc, cad_import, column_cat, cfg):
    """Scans one CAD layer (column_cat may be a single Category or a
    LayerSelection spanning several real layers) for column footprints
    (true circles + closed rect/polygon outlines). Returns a list of
    shapes exposing .contains(x, y) and .center -- used only by
    stitch_continuous to decide whether a gap between two collinear
    beam segments runs through a column."""
    from Autodesk.Revit.DB import Options
    from pp_common.cad_read import collect_shape_entities

    opts = Options()
    opts.IncludeNonVisibleObjects = True
    geom = cad_import.get_Geometry(opts)
    buckets = collect_shape_entities(doc, geom, column_cat,
                                     include_bound_arc_chords=True)

    shapes = []
    for cx, cy, r in buckets['circles']:
        shapes.append(_CircleFootprint((cx, cy), r))

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
        fp = Footprint(pts, rect_area_ratio=cfg.RECT_AREA_RATIO,
                      rotation_snap_deg=cfg.ROTATION_SNAP_DEG)
        if fp.width_ft < min_side or fp.length_ft > max_side:
            continue
        shapes.append(fp)

    return shapes


# ---------------------------------------------------------------------------
# Continuity: stitch collinear segments across gaps
# ---------------------------------------------------------------------------

def _endpoint_gap(seg_a, seg_b):
    """Returns (distance_ft, pt_a, pt_b) for whichever endpoint pair
    between the two segments is closest -- the real physical gap
    between them (not a projection-based approximation), used both to
    measure the gap distance and to test column-bridging at its actual
    location."""
    candidates = [(seg_a.p0, seg_b.p0), (seg_a.p0, seg_b.p1),
                  (seg_a.p1, seg_b.p0), (seg_a.p1, seg_b.p1)]
    best = min(candidates, key=lambda pq: (pq[0][0] - pq[1][0]) ** 2
                                          + (pq[0][1] - pq[1][1]) ** 2)
    d = math.hypot(best[0][0] - best[1][0], best[0][1] - best[1][1])
    return d, best[0], best[1]


def _find_bridging_column(pt_a, pt_b, column_footprints):
    """A column counts as spanning the gap when it contains the gap's
    midpoint or either near endpoint -- correct for the usual case (a
    column centered in the gap), not a general line-clip intersection
    test; a column badly off-center in an oversized gap could be missed
    (reported honestly as a plain 'gap below tolerance? no' skip, not
    silently guessed)."""
    mid = ((pt_a[0] + pt_b[0]) / 2.0, (pt_a[1] + pt_b[1]) / 2.0)
    for shape in column_footprints:
        if shape.contains(*mid) or shape.contains(*pt_a) or shape.contains(*pt_b):
            return shape
    return None


def _build_span(segs_group):
    """The stitched centerline for a connected group of collinear
    segments: the two most extreme REAL endpoints (not a projected/
    reconstructed point), found by projecting every endpoint in the
    group onto the first segment's own line and keeping the actual
    point at each extreme."""
    ref = segs_group[0]
    best_min = None
    best_max = None
    for seg in segs_group:
        for pt in (seg.p0, seg.p1):
            along, _perp = ref.project(pt)
            if best_min is None or along < best_min[0]:
                best_min = (along, pt)
            if best_max is None or along > best_max[0]:
                best_max = (along, pt)
    return Segment(best_min[1], best_max[1])


def _finalize_span(segs, centerlines, indices):
    group_segs = [segs[i] for i in indices]
    span_seg = _build_span(group_segs)
    widths = [centerlines[i]['width_ft'] for i in indices
             if centerlines[i]['width_ft'] is not None]
    width_ft = max(widths) if widths else None
    width_varied = len(set(round(w, 4) for w in widths)) > 1 if widths else False
    return {
        'segment': span_seg,
        'source_indices': sorted(i + 1 for i in indices),
        'width_ft': width_ft,
        'width_varied': width_varied,
    }


def stitch_continuous(centerlines, column_footprints, cfg):
    """Groups centerlines (from scan_beam_layer, 1-indexed in the
    caller's report) in two phases:

    1. Group every segment lying on the same infinite line (is_collinear,
       union-find) -- a pure "same line" test, position along the line
       doesn't matter yet. A non-collinear crossing (T-junction) never
       joins a group here (different angle), so it stays a distinct
       beam automatically.
    2. WITHIN each same-line group, order members along the shared
       direction and test ONLY consecutive neighbours for stitching
       (small gap, or a column footprint bridging it). Testing every
       pair in the group (not just neighbours) was tried first and
       confirmed live to misreport: a column near one real joint could
       also satisfy the "contains the gap midpoint" test for a totally
       unrelated, much larger gap between two non-adjacent segments on
       the same line, producing a nonsensical "column bridges a 5m gap"
       merge log entry even though the actual connectivity result
       happened to still be correct via the real intermediate joint.
       Restricting to neighbours removes that false report entirely.

    Returns (spans, merges) where spans is [{'segment', 'source_indices'
    (1-based, matching the Apply-time report), 'width_ft' (max of the
    merged segments' widths, None if all None), 'width_varied' (bool)}]
    and merges is [{'a_index', 'b_index', 'reason', 'gap_ft',
    'column_center'}] -- one entry per stitch actually made, for the
    Apply-time report."""
    segs = [cl['segment'] for cl in centerlines]
    n = len(segs)
    angle_tol_deg = cfg.COLLINEAR_ANGLE_TOL_DEG
    offset_tol_ft = cfg.COLLINEAR_OFFSET_TOL_MM * cfg.MM_TO_FT
    stitch_gap_ft = cfg.STITCH_GAP_TOLERANCE_MM * cfg.MM_TO_FT

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if is_collinear(segs[i], segs[j], angle_tol_deg, offset_tol_ft):
                union(i, j)

    line_groups = {}
    for i in range(n):
        line_groups.setdefault(find(i), []).append(i)

    spans = []
    merges = []
    for indices in line_groups.values():
        ref = segs[indices[0]]
        ordered = sorted(indices, key=lambda i: min(
            ref.project(segs[i].p0)[0], ref.project(segs[i].p1)[0]))

        current = [ordered[0]]
        for k in range(1, len(ordered)):
            prev_idx, cur_idx = current[-1], ordered[k]
            gap_ft, pt_a, pt_b = _endpoint_gap(segs[prev_idx], segs[cur_idx])
            reason = None
            bridging_col = None
            if gap_ft <= stitch_gap_ft:
                reason = "gap below tolerance"
            else:
                bridging_col = _find_bridging_column(pt_a, pt_b, column_footprints)
                if bridging_col is not None:
                    reason = "bridged by column"

            if reason is not None:
                merges.append({
                    'a_index': prev_idx + 1, 'b_index': cur_idx + 1,
                    'reason': reason, 'gap_ft': gap_ft,
                    'column_center': bridging_col.center if bridging_col else None,
                })
                current.append(cur_idx)
            else:
                spans.append(_finalize_span(segs, centerlines, current))
                current = [cur_idx]
        spans.append(_finalize_span(segs, centerlines, current))

    return spans, merges


# ---------------------------------------------------------------------------
# Label matching (reuses pp_common.labels -- the same Hungarian-
# assignment pipeline proven on Auto Foundation/Pile/Column), adapted
# for an open centerline instead of a closed shape: no interior to test
# a label against, so contains() is always False and dist_to_boundary()
# measures distance to the nearest point ON the segment (clamped to its
# endpoints), not to an infinite line.
# ---------------------------------------------------------------------------

class BeamSpanShape(object):
    """Wraps one continuous span (from stitch_continuous) with the
    duck-typed interface pp_common.labels.match_labels expects."""

    display_name = "beam"

    def __init__(self, span):
        self.span = span
        self.segment = span['segment']
        self.width_ft = span['width_ft']
        self.length_ft = self.segment.length
        self.source_indices = span['source_indices']
        self.width_varied = span['width_varied']
        self.label = None
        self.material = None

    def contains(self, x, y):
        return False

    def dist2_to_center(self, x, y):
        cx, cy = self.segment.midpoint
        return (x - cx) ** 2 + (y - cy) ** 2

    def dist_to_boundary(self, x, y):
        along, perp = self.segment.project((x, y))
        if 0.0 <= along <= self.segment.length:
            return abs(perp)
        end = self.segment.p0 if along < 0.0 else self.segment.p1
        return math.hypot(x - end[0], y - end[1])


def default_beam_label(shape):
    """Size-derived fallback name, used exactly like the point-placed
    tools' equivalents: a beam that ends up unmatched gets a name from
    its own geometry (b x length, or just length when b is unknown --
    1-line mode with no table yet) instead of a bare 'X1'."""
    def mm(value_ft):
        return int(round(value_ft / MM_TO_FT))
    if shape.width_ft is not None:
        return u"B{}x{}".format(mm(shape.width_ft), mm(shape.length_ft))
    return u"BEAM{}".format(mm(shape.length_ft))


def match_and_group_spans(spans, texts, cfg):
    """Wraps each span in a BeamSpanShape, matches CAD labels, and
    groups by label. Mirrors Auto Pile/Column's match_and_group: never
    adopts a same-length/width sibling's label (adopt_same_size_label=
    False) -- two beams sharing a cross-section width doesn't mean they
    share a design label, and length is essentially unique per span
    anyway.

    Returns (matched_count, OrderedDict label -> [BeamSpanShape])."""
    shapes = [BeamSpanShape(s) for s in spans]
    matched = pp_labels.match_labels(
        shapes, texts,
        label_max_distance_mm=cfg.LABEL_MAX_DISTANCE_MM,
        size_group_round_mm=cfg.SIZE_GROUP_ROUND_MM,
        adopt_same_size_label=False,
        synthetic_label_fn=default_beam_label)
    groups = pp_labels.group_shapes(shapes)
    return matched, groups


def group_spans_by_size_only(spans):
    """No-label mode: skip CAD text entirely and group purely by each
    span's own derived name. Returns OrderedDict label -> [BeamSpanShape]."""
    shapes = [BeamSpanShape(s) for s in spans]
    for s in shapes:
        s.label = default_beam_label(s)
    return pp_labels.group_shapes(shapes)


# ---------------------------------------------------------------------------
# Material reconciliation (PRIMARY cue: beam layer name: SECONDARY cue:
# label prefix -- see ab_config.py's *_LAYER_PATTERNS/*_LABEL_PATTERNS)
# ---------------------------------------------------------------------------

def classify_layer_material(layer_name, cfg):
    """Returns 'concrete', 'steel', 'timber', or None (no pattern
    matched, or more than one did -- ambiguous) from the beam layer's
    own name alone."""
    upper = (layer_name or "").upper()
    hits = []
    if any(p.upper() in upper for p in cfg.CONCRETE_LAYER_PATTERNS):
        hits.append('concrete')
    if any(p.upper() in upper for p in cfg.STEEL_LAYER_PATTERNS):
        hits.append('steel')
    if any(p.upper() in upper for p in cfg.TIMBER_LAYER_PATTERNS):
        hits.append('timber')
    return hits[0] if len(hits) == 1 else None


def classify_label_material(label, cfg):
    """Returns 'concrete', 'steel', 'timber', or None (no pattern
    matched, or more than one did -- ambiguous) from the label text
    alone."""
    if not label:
        return None
    upper = label.upper()
    hits = []
    if any(re.match(p, upper) for p in cfg.CONCRETE_LABEL_PATTERNS):
        hits.append('concrete')
    if any(re.match(p, upper) for p in cfg.STEEL_LABEL_PATTERNS):
        hits.append('steel')
    if any(re.match(p, upper) for p in cfg.TIMBER_LABEL_PATTERNS):
        hits.append('timber')
    return hits[0] if len(hits) == 1 else None


def reconcile_material(layer_name, label, cfg):
    """Combines the beam layer's name (PRIMARY cue) with the label
    prefix (SECONDARY cue). Returns (verdict, reason) where verdict is
    'concrete', 'steel', 'timber', or 'UNCLASSIFIED'. Never guesses: a
    label and layer that actively disagree, or a beam with neither cue
    conclusive, comes back UNCLASSIFIED -- the UI exposes a manual
    material picker for those."""
    layer_material = classify_layer_material(layer_name, cfg)
    label_material = classify_label_material(label, cfg)

    if layer_material and label_material:
        if layer_material == label_material:
            return layer_material, "layer + label agree"
        return ("UNCLASSIFIED",
               "layer reads {} but label reads {}".format(layer_material, label_material))
    if layer_material and not label_material:
        return layer_material, "layer name (label absent or ambiguous)"
    if label_material and not layer_material:
        return label_material, "label prefix (layer name absent or ambiguous)"
    return "UNCLASSIFIED", "layer name and label prefix both absent/ambiguous"


# ---------------------------------------------------------------------------
# Beam table parsing (M5 -- biggest risk in this tool). A schedule table
# is just a grid of text entities positioned in rows/columns; there is
# no line/box geometry to rely on (some tables have grid lines, many
# don't, and CAD table borders are inconsistent enough not to trust).
# Parsing is purely positional: cluster text by Y into rows and by X
# into columns, find the header row by keyword, read the rest as data.
# ALWAYS report the parsed grid back for verification before using it
# (see ab_config.py's TABLE_* tolerances) -- this is an OCR-like parse
# of arbitrary CAD text, not a guaranteed-correct structured read.
# ---------------------------------------------------------------------------

def _cluster_1d(values, tol):
    """Groups values into clusters where each value joins the nearest
    existing cluster if within tol of its running mean, else starts a
    new one (same greedy approach already used for BAJA marker
    clustering in Auto Column's ac_shapes.py). Returns clusters sorted
    by mean ascending: [(mean, [original_indices]), ...]."""
    clusters = []
    order = sorted(range(len(values)), key=lambda i: values[i])
    for i in order:
        v = values[i]
        placed = False
        for c in clusters:
            if abs(v - c[0]) <= tol:
                c[1].append(i)
                c[0] = sum(values[j] for j in c[1]) / len(c[1])
                placed = True
                break
        if not placed:
            clusters.append([v, [i]])
    clusters.sort(key=lambda c: c[0])
    return clusters


def _parse_number(text):
    """Parses a table cell as a plain number (mm), tolerant of a comma
    decimal separator and stray non-numeric characters (units, spaces).
    Returns None if nothing numeric could be extracted -- reported as
    an unparsed row rather than silently treated as 0."""
    cleaned = (text or "").strip().replace(',', '.')
    m = re.search(r'-?\d+(\.\d+)?', cleaned)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_beam_table(texts, cfg):
    """texts: [(x, y, value)] collected from the user's windowed
    region (any layer -- a schedule table's own text is not necessarily
    on the beam layer). Clusters into rows (Y, top-to-bottom since CAD
    Y increases upward) and columns (X), treats the first row as the
    header and identifies which column is the label/b/h by an EXACT
    match against the header hints (a substring match would false-
    positive constantly for single-letter hints like "B" -- "LABEL"
    itself contains a "B").

    Returns (table, report). table is {label: (b_mm, h_mm)} (first
    occurrence wins; a later row disagreeing with an earlier one for
    the same label is never silently overwritten -- see 'conflicts').
    report: {'rows': [[cell_text,...],...] (the full grid, header row
    first, for the user to visually verify against the CAD),
    'label_col', 'b_col', 'h_col' (0-based indices or None),
    'conflicts' ([label,...]), 'unparsed_rows' ([row_index,...] where
    b or h could not be read as a number), 'n_rows', 'n_cols'}."""
    if not texts:
        return {}, {'rows': [], 'label_col': None, 'b_col': None,
                    'h_col': None, 'conflicts': [], 'unparsed_rows': [],
                    'n_rows': 0, 'n_cols': 0}

    xs = [t[0] for t in texts]
    ys = [t[1] for t in texts]
    row_tol_ft = cfg.TABLE_ROW_CLUSTER_TOL_MM * cfg.MM_TO_FT
    col_tol_ft = cfg.TABLE_COL_CLUSTER_TOL_MM * cfg.MM_TO_FT

    row_clusters = _cluster_1d(ys, row_tol_ft)
    col_clusters = _cluster_1d(xs, col_tol_ft)
    row_clusters.sort(key=lambda c: -c[0])  # top-to-bottom
    n_rows, n_cols = len(row_clusters), len(col_clusters)

    row_of = {}
    for ri, (_mean, idxs) in enumerate(row_clusters):
        for i in idxs:
            row_of[i] = ri
    col_of = {}
    for ci, (_mean, idxs) in enumerate(col_clusters):
        for i in idxs:
            col_of[i] = ci

    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for i, (_x, _y, val) in enumerate(texts):
        r, c = row_of[i], col_of[i]
        grid[r][c] = (grid[r][c] + " " + val).strip() if grid[r][c] else val

    header = grid[0] if grid else []
    b_hints = [h.upper() for h in cfg.TABLE_B_HEADER_HINTS]
    h_hints = [h.upper() for h in cfg.TABLE_H_HEADER_HINTS]
    label_hints = [h.upper() for h in cfg.TABLE_LABEL_HEADER_HINTS]

    b_col = h_col = label_col = None
    for ci, cell in enumerate(header):
        upper = cell.upper().strip()
        if b_col is None and upper in b_hints:
            b_col = ci
        elif h_col is None and upper in h_hints:
            h_col = ci
    for ci, cell in enumerate(header):
        if ci in (b_col, h_col):
            continue
        upper = cell.upper().strip()
        if label_col is None and upper in label_hints:
            label_col = ci
    if label_col is None:
        remaining = [ci for ci in range(n_cols) if ci not in (b_col, h_col)]
        if len(remaining) == 1:
            label_col = remaining[0]

    table = {}
    conflicts = []
    unparsed_rows = []
    if label_col is not None and b_col is not None and h_col is not None:
        for ri in range(1, n_rows):
            row = grid[ri]
            label = (row[label_col] if label_col < len(row) else "").strip()
            if not label:
                continue
            b_mm = _parse_number(row[b_col]) if b_col < len(row) else None
            h_mm = _parse_number(row[h_col]) if h_col < len(row) else None
            if b_mm is None or h_mm is None:
                unparsed_rows.append(ri)
                continue
            if label in table:
                if table[label] != (b_mm, h_mm):
                    conflicts.append(label)
                continue
            table[label] = (b_mm, h_mm)

    report = {
        'rows': grid, 'label_col': label_col, 'b_col': b_col, 'h_col': h_col,
        'conflicts': conflicts, 'unparsed_rows': unparsed_rows,
        'n_rows': n_rows, 'n_cols': n_cols,
    }
    return table, report


def parse_number_mm(text):
    """Public alias for _parse_number -- used by script.py to re-read the
    BEAM TABLE grid's current (possibly user-edited) cell text back into
    a number, without reaching into the parser's own "private" helper."""
    return _parse_number(text)


# ---------------------------------------------------------------------------
# Type-list sizing (M6): decide each label's b/h and how it was sourced.
# Mirrors the spec's SIZING rule for concrete/timber exactly: table wins
# if the label is in it; otherwise a 2-line pairing's own gap gives b
# (h always has no CAD source and stays manual); otherwise both are
# fully manual (1-line mode with no table). Steel profile snapping is
# deliberately NOT done here -- it lands with Generate (M7), same as
# every other tool's steel-catalog snap.
# ---------------------------------------------------------------------------

_LABEL_SIZE_RE = re.compile(r'^([A-Z]+)(\d)(A)?(\d)(A)?$')


def decode_size_from_label(label, cfg):
    """Indonesian beam-label naming convention (confirmed against a
    real project, not guessed): <prefix><d1>[A]<d2>[A], where d1/d2 are
    single digits each worth 100mm, and an 'A' immediately following a
    digit adds 50mm to THAT digit's dimension -- position matters
    (B26A's A follows d2 -> +50 to h; B3A5's A follows d1 -> +50 to b;
    B3A5A has both -> +50 to both). Examples verified: B25->200x500,
    B57->500x700, B26A->200x650, B3A5->350x500, B3A5A->350x550,
    G36->300x600.

    Only applies to concrete/timber-style labels -- steel labels (WF/
    IWF/H) use an entirely different convention (explicit b/h in the
    label itself, e.g. WF300X150) and are excluded here rather than
    risk a false decode. Returns (b_mm, h_mm) or None if the label
    doesn't match this pattern at all (a synthetic size-derived label
    like 'B300x2870' never matches -- it has an 'x' and more than two
    digits, so this never overrides a real measurement with itself)."""
    if classify_label_material(label, cfg) == 'steel':
        return None
    m = _LABEL_SIZE_RE.match((label or "").upper())
    if not m:
        return None
    _prefix, d1, a1, d2, a2 = m.groups()
    # floats, not ints -- every other size value in this tool (2-line
    # gap measurements, table values) is a float, and pp_units.fmt_num
    # (used to display b/h in the BEAM TYPES grid) calls "{:.4f}".format()
    # on it; under IronPython that raises "Precision not allowed in
    # integer format specifier" when given a genuine int, which was
    # silently swallowed by initialize_session's try/except and left
    # the whole grid empty -- found via the live error.log, not caught
    # by testing resolve_type_sizing() in isolation (which never
    # exercised the downstream fmt_num() call).
    b_mm = float(int(d1) * 100 + (50 if a1 else 0))
    h_mm = float(int(d2) * 100 + (50 if a2 else 0))
    return b_mm, h_mm


def resolve_type_sizing(label, shapes, table, cfg):
    """shapes: the BeamSpanShape list for one label group. table: {label:
    (b_mm, h_mm)} as currently shown (and possibly hand-edited) in the
    BEAM TABLE grid. Returns (b_mm-or-None, h_mm-or-None, source_note).

    Priority: (1) beam table match always wins -- it's the authoritative,
    user-verified source. (2) decode_size_from_label -- the label ITSELF
    encodes b/h under a confirmed naming convention; reconciled against
    the 2-line gap measurement when both exist (agreement noted, a real
    disagreement surfaced rather than silently picked one way, same
    reconcile-not-guess pattern as material classification). (3) b from
    the 2-line gap alone + h defaulted to cfg.DEFAULT_BEAM_HEIGHT_MM (a
    starting value, not a measurement -- clearly labelled as such, and
    fully editable in the BEAM TYPES grid before Generate). (4) fully
    manual when nothing at all is available. Set DEFAULT_BEAM_HEIGHT_MM
    to None to go back to leaving h blank at tier 3."""
    default_h = getattr(cfg, 'DEFAULT_BEAM_HEIGHT_MM', None)
    if label in table:
        b_mm, h_mm = table[label]
        return b_mm, h_mm, "from beam table"

    widths = [s.width_ft for s in shapes if s.width_ft is not None]
    b_gap_mm = max(widths) / MM_TO_FT if widths else None

    decoded = decode_size_from_label(label, cfg)
    if decoded is not None:
        b_mm, h_mm = decoded
        if b_gap_mm is not None:
            tol_mm = cfg.COLLINEAR_OFFSET_TOL_MM if hasattr(cfg, 'COLLINEAR_OFFSET_TOL_MM') else 10.0
            if abs(b_mm - b_gap_mm) <= max(tol_mm, 0.1 * b_mm):
                return b_mm, h_mm, "from label suffix (agrees with 2-line gap)"
            return b_mm, h_mm, (
                "from label suffix; DISAGREES with 2-line gap ({:.0f}mm) -- "
                "verify".format(b_gap_mm))
        return b_mm, h_mm, "from label suffix (no 2-line gap to cross-check)"

    if default_h is not None:
        b_note = "b from 2-line gap" if b_gap_mm is not None else "b has no CAD source -- enter manually"
        return b_gap_mm, default_h, "{}; h defaulted to {:.0f}mm -- EDIT to confirm real depth".format(
            b_note, default_h)
    if b_gap_mm is not None:
        return b_gap_mm, None, "b from 2-line gap; h has no CAD source -- enter manually"
    return None, None, "no table match, no 2-line gap -- enter b and h manually"


def collect_texts_in_region(doc, min_xy, max_xy):
    """All CAD text within the (min_xy, max_xy) rectangle (feet, model
    coords), regardless of which CAD import or layer it lives on -- a
    beam-table pick doesn't presuppose one particular layer (the table
    is very often on its own dimension/annotation layer, unrelated to
    the beam or label layers already picked). Uses the DXF-export text
    reader with no layer filter (Revit's geometry API exposes no text
    for imported/linked DWGs at all -- confirmed repeatedly elsewhere in
    this extension, so there's no separate geometry-API attempt to make
    here either)."""
    from pp_common import dxf_text as pp_dxf
    raw_texts = pp_dxf.read_cad_texts(doc, None)
    (x0, y0), (x1, y1) = min_xy, max_xy
    xlo, xhi = min(x0, x1), max(x0, x1)
    ylo, yhi = min(y0, y1), max(y0, y1)
    return [(x, y, v) for x, y, v in raw_texts if xlo <= x <= xhi and ylo <= y <= yhi]
