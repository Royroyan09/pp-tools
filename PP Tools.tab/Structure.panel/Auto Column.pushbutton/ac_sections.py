# -*- coding: utf-8 -*-
"""Steel section snapping for Auto Column.

No SNI (Indonesian standard) section library exists on this machine —
searched every folder under both installed Revit versions' Libraries
for "sni" in any path/filename, zero hits. Falls back to AISC
throughout, per the spec (ac_config.STEEL_LIBRARY_ROOTS).

Revit's own AISC steel column families ship with a plain-text type
catalog (Width/Height or Diameter, in millimeters, one row per standard
rolled section) alongside the .rfa. That catalog is parsed directly —
far cheaper than instantiating every family type to read its
parameters, and it *is* the authoritative dimension source the family
itself is built from, so there's nothing to double-check by opening
the family too.
"""
import csv
import os

import ac_config as cfg


def _parse_type_catalog(path, dim_keys):
    """Parses a Revit family type-catalog .txt file. dim_keys: ordered
    [(catalog_header_name, output_key)] pairs identifying which columns
    to keep (matched against the part of each header before '##').
    Returns [{'name': str, output_key: float_mm, ...}, ...]; rows
    missing any requested dimension are skipped rather than raising, so
    a slightly different catalog layout degrades gracefully instead of
    failing the whole load."""
    rows = []
    with open(path, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)
        col_index = {}
        for i, col in enumerate(header):
            name = col.split('##')[0].strip()
            if name:
                col_index[name] = i

        for line in reader:
            if not line:
                continue
            entry = {'name': line[0]}
            ok = True
            for header_key, out_key in dim_keys:
                idx = col_index.get(header_key)
                if idx is None or idx >= len(line):
                    ok = False
                    break
                try:
                    entry[out_key] = float(line[idx])
                except ValueError:
                    ok = False
                    break
            if ok:
                rows.append(entry)
    return rows


def _find_first(file_candidates, roots):
    """Returns the first match for any candidate filename, searching
    roots (already specific leaf directories, checked IN ORDER — not a
    single merged directory walk, so a preferred root always wins over
    a same-named file found deeper in a lower-priority one)."""
    for root in roots:
        if not os.path.isdir(root):
            continue
        for fn in os.listdir(root):
            if fn in file_candidates:
                return os.path.join(root, fn)
    return None


class SteelSectionCatalog(object):
    """Lazily loads and caches one shape family's type catalog, and
    snaps an arbitrary bounding box to its nearest standard section by
    plain Euclidean distance in mm across the requested dimensions."""

    def __init__(self, family_file_candidates, catalog_file_candidates,
                dim_keys, library_roots):
        self.family_file_candidates = family_file_candidates
        self.catalog_file_candidates = catalog_file_candidates
        self.dim_keys = dim_keys
        self.library_roots = library_roots
        self._rows = None
        self._catalog_path = None
        self._family_path = None

    def _ensure_loaded(self):
        if self._rows is not None:
            return
        self._catalog_path = _find_first(self.catalog_file_candidates, self.library_roots)
        self._rows = (_parse_type_catalog(self._catalog_path, self.dim_keys)
                      if self._catalog_path else [])
        self._family_path = _find_first(self.family_file_candidates, self.library_roots)

    def family_path(self):
        self._ensure_loaded()
        return self._family_path

    def catalog_path(self):
        self._ensure_loaded()
        return self._catalog_path

    def row_count(self):
        self._ensure_loaded()
        return len(self._rows)

    def row_by_name(self, name):
        """Looks up a catalog row by its exact section name (case-
        insensitive) -- used when a shape has no real bounding box of
        its own (e.g. a footprint-less symbol column) but the user has
        typed/confirmed a Section, to get real dimensions for rotation-
        axis detection. Returns the row dict, or None."""
        self._ensure_loaded()
        wanted = (name or "").strip().lower()
        for row in self._rows:
            if row['name'].strip().lower() == wanted:
                return row
        return None

    def snap(self, **target_mm):
        """target_mm: keyword args matching this catalog's dim_keys
        output keys (e.g. width_mm=305, height_mm=610, or diameter_mm=
        219 for the round catalog). Returns (matched_row, distance_mm)
        — matched_row is {'name', <dim>_mm...}; (None, None) if no
        catalog could be found or parsed."""
        self._ensure_loaded()
        if not self._rows:
            return None, None
        best, best_d2 = None, None
        for row in self._rows:
            d2 = sum((row[k] - v) ** 2 for k, v in target_mm.items())
            if best_d2 is None or d2 < best_d2:
                best, best_d2 = row, d2
        return best, (best_d2 ** 0.5 if best_d2 is not None else None)


W_SHAPES = SteelSectionCatalog(
    cfg.W_SHAPE_FAMILY_FILE_CANDIDATES, cfg.W_SHAPE_CATALOG_FILE_CANDIDATES,
    [('Width', 'width_mm'), ('Height', 'height_mm')], cfg.STEEL_LIBRARY_ROOTS)

C_SHAPES = SteelSectionCatalog(
    cfg.C_SHAPE_FAMILY_FILE_CANDIDATES, cfg.C_SHAPE_CATALOG_FILE_CANDIDATES,
    [('Width', 'width_mm'), ('Height', 'height_mm')], cfg.STEEL_LIBRARY_ROOTS)

HSS_RECT = SteelSectionCatalog(
    cfg.HSS_RECT_FAMILY_FILE_CANDIDATES, cfg.HSS_RECT_CATALOG_FILE_CANDIDATES,
    [('Width', 'width_mm'), ('Height', 'height_mm')], cfg.STEEL_LIBRARY_ROOTS)

HSS_SQUARE = SteelSectionCatalog(
    cfg.HSS_SQUARE_FAMILY_FILE_CANDIDATES, cfg.HSS_SQUARE_CATALOG_FILE_CANDIDATES,
    [('Width', 'width_mm'), ('Height', 'height_mm')], cfg.STEEL_LIBRARY_ROOTS)

HSS_ROUND = SteelSectionCatalog(
    cfg.HSS_ROUND_FAMILY_FILE_CANDIDATES, cfg.HSS_ROUND_CATALOG_FILE_CANDIDATES,
    [('Diameter', 'diameter_mm')], cfg.STEEL_LIBRARY_ROOTS)


def catalog_for_shape(shape_kind, width_mm, length_mm):
    """Dispatches to the right catalog by shape display_name ('i_h'/'I/H'
    -> W-shapes, 'channel' -> C-shapes, 'hollow' -> HSS square or
    rectangular depending on how close to square the outer profile is,
    'rect' -> W-shapes too). Returns the SteelSectionCatalog, or None
    for 'circle'/'custom' (no steel catalog applies).

    'rect' only reaches here when the user has manually marked a plain-
    rectangle column steel (Auto Column never guesses this on its own --
    see reconcile_material) -- confirmed against a real drawing where
    Wide Flange columns are drawn as just their bounding rectangle in
    plan, with the true H-profile shown only in a separate detail view.
    Wide Flange is assumed as the more common case; type a different
    Section by hand first (channel/HSS) if that's what it actually is --
    the section text, not this dispatch, decides the family that's
    searched at Generate time.

    Shared by both the Apply-time snap (snap_column_shape) and Generate-
    time family lookup, so the square-vs-rectangular HSS decision is
    made exactly once."""
    if shape_kind in ("I/H", "i_h", "rect"):
        return W_SHAPES
    if shape_kind == "channel":
        return C_SHAPES
    if shape_kind == "hollow":
        is_square = abs(width_mm - length_mm) <= cfg.HOLLOW_MIN_WALL_MM
        return HSS_SQUARE if is_square else HSS_RECT
    return None


def snap_column_shape(shape, width_mm, length_mm):
    """Dispatches to the right catalog by shape.display_name and snaps
    to the nearest standard section. Returns (catalog, matched_row,
    distance_mm) — matched_row/distance are None when no catalog was
    found."""
    catalog = catalog_for_shape(shape.display_name, width_mm, length_mm)
    if catalog is None:
        return None, None, None
    row, dist = catalog.snap(width_mm=width_mm, height_mm=length_mm)
    return catalog, row, dist
