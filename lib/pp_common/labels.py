# -*- coding: utf-8 -*-
"""CAD-label-to-shape matching shared by every auto-modelling tool.

Works against any "shape" object exposing the Footprint interface:
.contains(x, y), .dist_to_boundary(x, y), .dist2_to_center(x, y),
.width_ft, .length_ft, and a settable .label. pp_common.geometry.
Footprint satisfies this; other shapes (e.g. a circle wrapper) can too.
"""
from collections import OrderedDict

from pp_common import config_base
from pp_common.geometry import label_sort_key


def _size_key(fp, round_ft):
    return (int(round(fp.width_ft / round_ft)),
            int(round(fp.length_ft / round_ft)))


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


def match_labels(shapes, texts, label_max_distance_mm=None,
                 size_group_round_mm=None, unmatched_label_format=None,
                 adopt_same_size_label=True, synthetic_label_fn=None):
    """texts: [(x, y, value)] already cleaned/filtered. Four passes:

    1. a text whose insertion point falls INSIDE a shape labels it
    2. remaining texts/shapes are paired globally nearest-first by edge
       distance, one text per shape (Hungarian/optimal assignment) —
       labels with leaders sit outside their shape, and per-shape
       nearest matching lets a neighbour's label steal, so pairing must
       be globally optimal and one-to-one
    3. (optional, adopt_same_size_label=True by default) still-
       unlabelled shapes adopt the label of an already labelled shape
       with the same (rounded) size — drawings often label only one
       representative per type. Set adopt_same_size_label=False when a
       same-size match would be misleading rather than helpful (e.g.
       many identical-diameter piles under one labelled pile CAP,
       where the cap's label doesn't describe the individual pile).
    4. whatever is left gets a synthetic label, one per size: by
       default unmatched_label_format.format(n=...), or the result of
       synthetic_label_fn(shape) when given (lets a caller produce a
       size-derived name like "D800" instead of a bare "X1").

    Returns the number of shapes labelled from CAD text (passes 1+2)."""
    if size_group_round_mm is None:
        size_group_round_mm = config_base.SIZE_GROUP_ROUND_MM
    if unmatched_label_format is None:
        unmatched_label_format = config_base.UNMATCHED_LABEL_FORMAT
    if label_max_distance_mm is None:
        label_max_distance_mm = config_base.LABEL_MAX_DISTANCE_MM
    round_ft = size_group_round_mm * config_base.MM_TO_FT

    matched = 0
    used = set()

    # pass 1: insertion point inside the shape
    for fp in shapes:
        inside = [(i, t) for i, t in enumerate(texts)
                  if i not in used and fp.contains(t[0], t[1])]
        if inside:
            i, t = min(inside,
                       key=lambda it: fp.dist2_to_center(it[1][0], it[1][1]))
            fp.label = t[2]
            used.add(i)
            matched += 1

    # pass 2: optimal one-to-one pairing by edge distance (minimum total
    # cost, Hungarian). Nearest-first greedy is NOT enough: a small
    # shape next to a big one's leader label steals it even though the
    # globally cheapest pairing gives both their own text.
    max_d_ft = label_max_distance_mm * config_base.MM_TO_FT if label_max_distance_mm else None
    big = 1e9
    free_f = [i for i, fp in enumerate(shapes) if fp.label is None]
    free_t = [i for i in range(len(texts)) if i not in used]
    if free_f and free_t:
        cost = []
        for fi in free_f:
            row = []
            for ti in free_t:
                d = shapes[fi].dist_to_boundary(texts[ti][0], texts[ti][1])
                if max_d_ft is not None and d > max_d_ft:
                    d = big
                row.append(d)
            cost.append(row)
        # the solver needs rows <= columns; transpose when there are
        # more shapes than texts
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
            shapes[fi].label = texts[ti][2]
            used.add(ti)
            matched += 1

    # pass 3: adopt the label of a same-size labelled shape (skippable)
    if adopt_same_size_label:
        size_to_label = {}
        for fp in shapes:
            if fp.label is not None:
                size_to_label.setdefault(_size_key(fp, round_ft), fp.label)
        for fp in shapes:
            if fp.label is None:
                fp.label = size_to_label.get(_size_key(fp, round_ft))

    # pass 4: synthetic labels for whatever is still unmatched
    size_labels = {}
    for fp in shapes:
        if fp.label is not None:
            continue
        key = _size_key(fp, round_ft)
        if key not in size_labels:
            if synthetic_label_fn is not None:
                size_labels[key] = synthetic_label_fn(fp)
            else:
                size_labels[key] = unmatched_label_format.format(
                    n=len(size_labels) + 1)
        fp.label = size_labels[key]
    return matched


def group_shapes(shapes):
    """OrderedDict label -> [shape], labels naturally sorted."""
    groups = {}
    for fp in shapes:
        groups.setdefault(fp.label, []).append(fp)
    ordered = OrderedDict()
    for label in sorted(groups.keys(), key=label_sort_key):
        ordered[label] = groups[label]
    return ordered
