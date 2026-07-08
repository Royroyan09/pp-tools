# -*- coding: utf-8 -*-
"""Steel section snapping for Auto Beam.

Mirrors Auto Column's ac_sections.py exactly (same catalog-parsing
engine: Revit's AISC steel families ship a plain-text type catalog
alongside the .rfa, one row per standard rolled section, parsed
directly rather than instantiating every type). Not shared via
pp_common -- Auto Column's file is a working, already-verified module
and duplicating ~180 lines here carries far less regression risk than
refactoring it out from under a tool that must keep working.

The one real difference from Auto Column: a beam has no shape
signature at all (plan geometry is just lines -- see ab_config.py's
material-classification note), so there is no "I/H vs channel vs
hollow" shape test to dispatch on. The ONLY cue for which catalog to
snap against is the label prefix (ab_config.STEEL_CHANNEL_LABEL_
PATTERNS / STEEL_HSS_LABEL_PATTERNS); everything else defaults to
W-shapes, same default Auto Column uses for a plain rectangle marked
steel by hand.
"""
import csv
import os
import re

import ab_config as cfg


def _parse_type_catalog(path, dim_keys, unit_scale_to_mm=1.0):
    """Parses a Revit family type-catalog .txt file. dim_keys: ordered
    [(catalog_header_name, output_key)] pairs identifying which columns
    to keep (matched against the part of each header before '##').
    unit_scale_to_mm: multiplier applied to every parsed dimension --
    Revit's steel catalogs give millimetres directly (1.0), but the
    default timber catalog (M_Timber.txt) gives inches (confirmed at
    runtime by reading the file), so callers convert at parse time
    rather than carrying mixed units downstream. Returns [{'name': str,
    output_key: float_mm, ...}, ...]; rows missing any requested
    dimension are skipped rather than raising, so a slightly different
    catalog layout degrades gracefully instead of failing the whole
    load."""
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
                    entry[out_key] = float(line[idx]) * unit_scale_to_mm
                except ValueError:
                    ok = False
                    break
            if ok:
                rows.append(entry)
    return rows


def _find_first(file_candidates, roots):
    """Returns the first match for any candidate filename, searching
    roots (already specific leaf directories, checked IN ORDER -- not a
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
    snaps an arbitrary target size to its nearest standard section by
    plain Euclidean distance in mm across the requested dimensions."""

    def __init__(self, family_file_candidates, catalog_file_candidates,
                dim_keys, library_roots, unit_scale_to_mm=1.0):
        self.family_file_candidates = family_file_candidates
        self.catalog_file_candidates = catalog_file_candidates
        self.dim_keys = dim_keys
        self.library_roots = library_roots
        self.unit_scale_to_mm = unit_scale_to_mm
        self._rows = None
        self._catalog_path = None
        self._family_path = None

    def _ensure_loaded(self):
        if self._rows is not None:
            return
        self._catalog_path = _find_first(self.catalog_file_candidates, self.library_roots)
        self._rows = (_parse_type_catalog(self._catalog_path, self.dim_keys, self.unit_scale_to_mm)
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
        insensitive) -- used when the user has typed/confirmed a
        Section by hand with no real b to snap from. Returns the row
        dict, or None."""
        self._ensure_loaded()
        wanted = (name or "").strip().lower()
        for row in self._rows:
            if row['name'].strip().lower() == wanted:
                return row
        return None

    def snap(self, **target_mm):
        """target_mm: keyword args matching this catalog's dim_keys
        output keys (e.g. width_mm=305, height_mm=610). Returns
        (matched_row, distance_mm) -- (None, None) if no catalog could
        be found or parsed."""
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
    cfg.W_SHAPE_BEAM_FAMILY_FILE_CANDIDATES, cfg.W_SHAPE_BEAM_CATALOG_FILE_CANDIDATES,
    [('Width', 'width_mm'), ('Height', 'height_mm')], cfg.BEAM_STEEL_LIBRARY_ROOTS)

C_SHAPES = SteelSectionCatalog(
    cfg.C_SHAPE_BEAM_FAMILY_FILE_CANDIDATES, cfg.C_SHAPE_BEAM_CATALOG_FILE_CANDIDATES,
    [('Width', 'width_mm'), ('Height', 'height_mm')], cfg.BEAM_STEEL_LIBRARY_ROOTS)

HSS_RECT = SteelSectionCatalog(
    cfg.HSS_RECT_BEAM_FAMILY_FILE_CANDIDATES, cfg.HSS_RECT_BEAM_CATALOG_FILE_CANDIDATES,
    [('Width', 'width_mm'), ('Height', 'height_mm')], cfg.BEAM_STEEL_LIBRARY_ROOTS)

# M_Timber.txt's own header uses 'b'/'d' (not 'Width'/'Height' like the
# steel catalogs) and gives dimensions in inches -- both confirmed by
# reading the real file at runtime (see ab_config.py's
# TIMBER_CATALOG_UNIT_SCALE_TO_MM note).
TIMBER = SteelSectionCatalog(
    cfg.TIMBER_FAMILY_FILE_CANDIDATES, cfg.TIMBER_CATALOG_FILE_CANDIDATES,
    [('b', 'width_mm'), ('d', 'height_mm')], cfg.TIMBER_LIBRARY_ROOTS,
    unit_scale_to_mm=cfg.TIMBER_CATALOG_UNIT_SCALE_TO_MM)


def catalog_for_label(label):
    """Dispatches to the right catalog by LABEL PREFIX ONLY (no shape
    signature exists for a beam -- see the module docstring). Returns
    the SteelSectionCatalog; defaults to W-shapes when the label
    matches neither the channel nor HSS pattern, same default Auto
    Column applies to a hand-marked-steel plain rectangle."""
    upper = (label or "").upper()
    if any(re.match(p, upper) for p in cfg.STEEL_HSS_LABEL_PATTERNS):
        return HSS_RECT
    if any(re.match(p, upper) for p in cfg.STEEL_CHANNEL_LABEL_PATTERNS):
        return C_SHAPES
    return W_SHAPES


def snap_beam_size(label, width_mm, height_mm):
    """Dispatches by label prefix and snaps (width_mm, height_mm) to
    the nearest standard section. Returns (catalog, matched_row,
    distance_mm) -- matched_row/distance are None when no catalog was
    found or height_mm is unavailable (h not yet entered)."""
    catalog = catalog_for_label(label)
    if catalog is None or height_mm is None:
        return catalog, None, None
    row, dist = catalog.snap(width_mm=width_mm, height_mm=height_mm)
    return catalog, row, dist


def snap_timber_size(width_mm, height_mm):
    """Snaps (width_mm, height_mm) to the nearest standard timber
    section in the TIMBER catalog -- unlike steel, there is only the
    one catalog (no channel/HSS-style dispatch; a beam's plan geometry
    gives no shape cue to dispatch on anyway, and timber framing does
    not come in alternate profile families the way steel does).
    Returns (matched_row, distance_mm) -- (None, None) if no catalog
    was found or height_mm is unavailable (h not yet entered)."""
    if height_mm is None:
        return None, None
    return TIMBER.snap(width_mm=width_mm, height_mm=height_mm)
