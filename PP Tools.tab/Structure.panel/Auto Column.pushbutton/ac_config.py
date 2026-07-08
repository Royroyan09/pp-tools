# -*- coding: utf-8 -*-
"""Auto Column configuration.

Shared tunables (unit conversion, CAD tolerances, label matching,
placement) live in pp_common.config_base and are inherited here; only
Column-specific detection rules are defined below.
"""
from pp_common.config_base import *

# ---------------------------------------------------------------------------
# Material classification — PRIMARY cue: label prefix
# ---------------------------------------------------------------------------

# Regex patterns checked against the UPPERCASED label text (re.match,
# so they anchor at the start). A label matching neither list, or
# matching both, is ambiguous by label alone -- shape signature (below)
# is then the deciding/secondary cue. Genuinely editable: add local
# office conventions here.
#
# "C" prefix is deliberately split: a bare short number (C3, C12) reads
# as a concrete column tag; a dimension-style suffix (C150X50, C100x50)
# reads as a steel channel size -- exactly the label ambiguity the spec
# calls out, resolved by pattern shape rather than the letter alone.
CONCRETE_LABEL_PATTERNS = [
    r'^K\d',       # K1, K12 ...
    r'^KP\d',      # KP1, KP2 ... (kolom pedestal)
    r'^KT\d',      # KT1, KT2 ... (kolom tangga -- stair column; verified
                   # against a real drawing where these were unclassified
                   # before this pattern was added)
    r'^C\d+$',     # C3, C12 (bare number, no dimension suffix)
]
STEEL_LABEL_PATTERNS = [
    r'^WF',                 # WF300x150...
    r'^H\d',                # H300x300...
    r'^HSS',                # HSS100x100x4.5
    r'^C\d+[Xx]',           # C150X50, C100x50x20 (channel dimension format)
]

# ---------------------------------------------------------------------------
# Shape signature classification — SECONDARY cue
# ---------------------------------------------------------------------------

# A closed outline whose area fills at least RECT_AREA_RATIO (from
# config_base) of its oriented bounding box is a plain rectangle —
# AMBIGUOUS by shape alone (concrete rect vs. hollow steel tube; the
# nested-loop check below is what actually flags "hollow").

# Fill-ratio band a concave outline must fall in to be considered an
# I/H or channel candidate at all (rules out noise/near-rectangles that
# just missed the RECT_AREA_RATIO cutoff, and rules out very thin/open
# shapes that are probably not a rolled steel section).
PROFILE_FILL_RATIO_RANGE = (0.15, 0.80)

# A reflected boundary sample point counts as "landing back on the
# outline" when it's within this fraction of the shape's SHORTER
# oriented-bbox side from the nearest edge. Two independent symmetry
# axes both holding -> I/H; exactly one -> channel; neither -> custom
# (L/T/other -- flagged, not generated; see ac_shapes.CustomColumnShape).
SYMMETRY_TOLERANCE_FRACTION = 0.06
SYMMETRY_SAMPLE_POINTS = 48

# Two closed outlines around the same center, inner one smaller by at
# least this much (mm) on both sides, count as a hollow tube (outer
# profile = the column's real size; the gap is checked to rule out
# duplicate/near-identical CAD lines being mistaken for a wall).
HOLLOW_MIN_WALL_MM = 2.0
# Nested outlines are only paired up if their centers are this close
# (mm) -- keeps unrelated nearby profiles (two separate columns) from
# being mistaken for one hollow tube.
HOLLOW_CENTER_TOLERANCE_MM = 30.0

# ---------------------------------------------------------------------------
# Concrete column families
# ---------------------------------------------------------------------------

RECT_CONCRETE_FAMILY_NAME_CANDIDATES = ["M_Concrete-Rectangular-Column", "Concrete-Rectangular-Column"]
RECT_CONCRETE_FAMILY_FILE_CANDIDATES = ["M_Concrete-Rectangular-Column.rfa", "Concrete-Rectangular-Column.rfa"]
ROUND_CONCRETE_FAMILY_NAME_CANDIDATES = ["M_Concrete-Round-Column", "Concrete-Round-Column"]
ROUND_CONCRETE_FAMILY_FILE_CANDIDATES = ["M_Concrete-Round-Column.rfa", "Concrete-Round-Column.rfa"]

CONCRETE_COLUMN_LIBRARY_ROOTS = [
    r"C:\ProgramData\Autodesk\RVT 2026\Libraries\English\US\Structural Columns\Concrete",
    r"C:\ProgramData\Autodesk\RVT 2025\Libraries\English\US\Structural Columns\Concrete",
    r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Columns\Concrete",
]

# Real parameter names confirmed at runtime (see ac_kinds.py), not
# assumed -- these are just the candidates to check.
COLUMN_WIDTH_PARAM_CANDIDATES = ["Width", "b"]
COLUMN_DEPTH_PARAM_CANDIDATES = ["Depth", "h"]
COLUMN_DIAMETER_PARAM_CANDIDATES = ["Diameter", "d", "b"]  # verified: M_Concrete-Round-Column
                                                            # literally calls its diameter "b"

# Structural material: optional, mirrors Auto Pile's ap_config.py --
# only set when the resolved family actually exposes a settable
# ElementId parameter matching one of these names (verified live:
# M_Concrete-Rectangular-Column has "Structural Material").
COLUMN_MATERIAL_PARAM_CANDIDATES = ["Structural Material", "Material"]
CONCRETE_MATERIAL_NAME_CANDIDATES = [
    "Concrete - Cast-in-Place Concrete",
    "Concrete, Cast-in-Place gray",
    "Concrete, Cast-in-Place",
    "Concrete",
]

# ---------------------------------------------------------------------------
# Steel column families + section snapping
# ---------------------------------------------------------------------------

# No SNI (Indonesian standard) section library exists on this machine —
# searched every folder under both installed Revit versions' Libraries
# for "sni" in any path/filename, zero hits. Falls back to AISC
# throughout, per the spec. Each shape's roots are listed in priority
# order (AISC 15.0 first, then 14.1, then the plain non-catalog family)
# rather than relying on directory-walk order to prefer the newer one.
_STEEL_ROOT_2025 = r"C:\ProgramData\Autodesk\RVT 2025\Libraries\English\US\Structural Columns\Steel"
_STEEL_ROOT_2026 = r"C:\ProgramData\Autodesk\RVT 2026\Libraries\English\US\Structural Columns\Steel"
STEEL_LIBRARY_ROOTS = [
    _STEEL_ROOT_2026 + r"\AISC 15.0",
    _STEEL_ROOT_2025 + r"\AISC 15.0",
    _STEEL_ROOT_2026 + r"\AISC 14.1",
    _STEEL_ROOT_2025 + r"\AISC 14.1",
    _STEEL_ROOT_2026,
    _STEEL_ROOT_2025,
]

W_SHAPE_CATALOG_FILE_CANDIDATES = ["M_W Shapes-Column.txt", "M_W-Wide Flange-Column.txt"]
W_SHAPE_FAMILY_FILE_CANDIDATES = ["M_W Shapes-Column.rfa", "M_W-Wide Flange-Column.rfa"]

C_SHAPE_CATALOG_FILE_CANDIDATES = ["M_C Shapes-Column.txt", "M_C-Channel-Column.txt"]
C_SHAPE_FAMILY_FILE_CANDIDATES = ["M_C Shapes-Column.rfa", "M_C-Channel-Column.rfa"]

HSS_RECT_CATALOG_FILE_CANDIDATES = ["M_HSS Rectangular-Column.txt", "M_HSS-Hollow Structural Section-Column.txt"]
HSS_RECT_FAMILY_FILE_CANDIDATES = ["M_HSS Rectangular-Column.rfa", "M_HSS-Hollow Structural Section-Column.rfa"]

HSS_SQUARE_CATALOG_FILE_CANDIDATES = ["M_HSS Square-Column.txt", "M_HSS-Hollow Structural Section-Column.txt"]
HSS_SQUARE_FAMILY_FILE_CANDIDATES = ["M_HSS Square-Column.rfa", "M_HSS-Hollow Structural Section-Column.rfa"]

HSS_ROUND_CATALOG_FILE_CANDIDATES = ["M_HSS Round-Column.txt", "CHS-Circular Hollow Section-Column.txt", "M_Pipe-Column.txt"]
HSS_ROUND_FAMILY_FILE_CANDIDATES = ["M_HSS Round-Column.rfa", "CHS-Circular Hollow Section-Column.rfa", "M_Pipe-Column.rfa"]

# ---------------------------------------------------------------------------
# Vertical constraints
# ---------------------------------------------------------------------------

# Top = the next level above the column's base level; when there is no
# level above (topmost level in the model), the column uses an
# unconnected height instead. This default is pre-filled per type in
# the UI and is fully editable there.
DEFAULT_UNCONNECTED_HEIGHT_MM = 3000.0

# ---------------------------------------------------------------------------
# Layer-name hints (used only to pre-select combo boxes; picking in the
# drawing always wins). Extend with local naming conventions.
# ---------------------------------------------------------------------------

PERIMETER_LAYER_HINTS = ["KOLOM", "COLS", "COLUMN", "STEEL"]
LABEL_LAYER_HINTS = ["LABEL", "TEXT", "NOTASI", "KETERANGAN", "MARK"]

# ---------------------------------------------------------------------------
# Steel symbol layer (optional) -- some drawings mark a steel column with
# only a small non-scale marker glyph and no real footprint at all (see
# ac_shapes.SymbolicSteelColumnShape/scan_symbol_layer; confirmed against
# a real drawing where certain steel columns have no closed outline
# anywhere on the perimeter layer).
# ---------------------------------------------------------------------------

STEEL_SYMBOL_LAYER_HINTS = ["BAJA", "WF", "STEEL"]

# A marker glyph's individual stroke fragments are clustered into one
# column if they lie within this distance (mm) of each other -- set
# well above the ~300-400mm gap measured between fragments of the same
# marker, and well below the multi-metre spacing between distinct
# columns, so it will not accidentally merge two real columns.
SYMBOL_CLUSTER_MAX_DISTANCE_MM = 1000.0

# A marker cluster is only treated as decorating an EXISTING footprint
# (and dropped, to avoid a duplicate instance) when it lands inside
# that footprint or within this small fixed distance (mm) of its edge.
# Deliberately small and fixed rather than scaled to the other shape's
# size: scaling by size once suppressed a real marker that happened to
# sit ~160mm outside an unrelated nearby rectangle several times its
# own size, wrongly treating two distinct columns as one -- confirmed
# against a real drawing.
SYMBOL_DEDUP_MARGIN_MM = 150.0

# ---------------------------------------------------------------------------
# Auto-join (after Generate)
# ---------------------------------------------------------------------------

# Concrete column join order (floor cuts column, column cuts framing) is
# confirmed by the user's spec and always runs. Steel-column join
# behavior was NOT specified/confirmed (no steel CAD data was available
# to validate against) -- off by default; flip to True once confirmed.
ENABLE_STEEL_COLUMN_JOIN = False

# Bounding-box expansion (mm) used to spatially prefilter candidate
# floors/framing near each placed column, before attempting a real join.
JOIN_SEARCH_MARGIN_MM = 300.0
