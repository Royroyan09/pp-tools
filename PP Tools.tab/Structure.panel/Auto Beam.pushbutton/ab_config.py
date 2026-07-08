# -*- coding: utf-8 -*-
"""Auto Beam configuration.

Shared tunables (unit conversion, CAD tolerances, label matching,
placement, and the new open-line/collinearity geometry added for this
tool) live in pp_common.config_base and are inherited here; only
Beam-specific detection rules are defined below as milestones add them.
"""
from pp_common.config_base import *

# ---------------------------------------------------------------------------
# Perimeter (beam edge/centerline) layer scanning
# ---------------------------------------------------------------------------

# Raw line/polyline-edge segments shorter than this (mm) are treated as
# noise (dimension ticks, hatching, text strokes) and skipped -- same
# role as MIN_FOOTPRINT_SIDE_MM for the point-placed tools, but there is
# no matching upper bound here: a real beam centerline can legitimately
# run the length of the building.
MIN_SEGMENT_LENGTH_MM = 200.0

# ---------------------------------------------------------------------------
# Layer-name hints (used only to pre-select combo boxes; picking in the
# drawing always wins). Extend with local naming conventions.
# ---------------------------------------------------------------------------

BEAM_LAYER_HINTS = ["BALOK", "BEAM", "GIRDER", "GB"]
LABEL_LAYER_HINTS = ["LABEL", "TEXT", "NOTASI", "KETERANGAN", "MARK"]

# Column layer used only to test whether a stitching gap is spanned by
# a real column (continuity through columns) -- NOT full column
# classification, that stays Auto Column's job. Aliases/equivalents so
# local naming conventions (KOL vs KOLOM vs COLUMN) still match.
COLUMN_LAYER_HINTS = ["KOLOM", "KOL", "COLUMN", "COLS"]

# ---------------------------------------------------------------------------
# Continuity (stitch collinear centerline segments into one continuous
# beam across gaps -- especially through columns)
# ---------------------------------------------------------------------------

# Two collinear segments stitch into one continuous span when the gap
# between their nearest endpoints is at or below this (mm) -- assumed
# to be drafting imprecision at a joint, not a real break -- OR when a
# column footprint spans the gap (checked regardless of this
# tolerance; see ab_shapes.stitch_continuous).
STITCH_GAP_TOLERANCE_MM = 50.0

# ---------------------------------------------------------------------------
# Material classification (concrete / steel / timber) -- PRIMARY cue:
# the picked beam layer's own NAME; SECONDARY cue: label prefix. Plan
# geometry is just lines here (no shape signature like Auto Column's
# I/H detection), so these two cues are all there is to reconcile --
# agree -> classify, conflict or neither conclusive -> UNCLASSIFIED
# (see ab_shapes.reconcile_material). Genuinely editable: add local
# office conventions here.
# ---------------------------------------------------------------------------

# "BALOK" is deliberately NOT a concrete pattern: it is just Indonesian
# for "beam" (a generic structural-element word, matching BEAM_LAYER_
# HINTS above), not a material -- a layer literally named "STR-PLAN
# BALOK" (confirmed to exist on a real drawing) carries no material
# information at all and must stay ambiguous, not default to concrete.
CONCRETE_LAYER_PATTERNS = ["BETON", "CONCRETE"]
STEEL_LAYER_PATTERNS = ["BAJA", "STEEL", "WF"]
TIMBER_LAYER_PATTERNS = ["KAYU", "TIMBER", "WOOD"]

# Checked against the UPPERCASED label text (re.match, so they anchor
# at the start). A label matching neither list, or matching more than
# one, is ambiguous by label alone -- the layer name (above) is then
# the deciding/other cue.
CONCRETE_LABEL_PATTERNS = [r'^B\d', r'^GB\d', r'^G\d']  # B1, B12, GB1 (girder
                                                        # balok), G36 (girder --
                                                        # confirmed against a
                                                        # real roof framing plan)
STEEL_LABEL_PATTERNS = [r'^WF', r'^IWF', r'^H\d']
# No real timber drawing has been tested against yet -- placeholder
# pattern, confirm/adjust once one is available.
TIMBER_LABEL_PATTERNS = [r'^BK\d']               # BK1, BK2 (balok kayu)

# ---------------------------------------------------------------------------
# Label text FILTER (applied before label matching even runs -- see
# script.py's on_apply, which already had this LABEL_REGEX hook from
# pp_common.config_base but left it unset). A picked label layer can
# carry more than beam callouts on it -- confirmed against a real
# drawing where K1/K4/K5A (column callouts), S12/S15 (slab callouts),
# and CS15/SC3/SH1A (other elements) sat on/near the same label text as
# genuine beam labels (G36, G37, G4A9, ...). Without this filter,
# pp_common.labels.match_labels has no way to know a piece of text
# ISN'T a beam label -- it just assigns whatever text is nearest to
# each span, so a column callout sitting close to a beam could get
# wrongly attached to that beam. This restricts matching to text that
# actually looks like one of the confirmed beam-label prefixes above
# (concrete + steel + timber combined); anything else (K/S/CS/SC/SH/...)
# is invisible to the matcher and that span falls back to its own
# size-derived default label instead of adopting the wrong text.
# ---------------------------------------------------------------------------

LABEL_REGEX = r'^(B|GB|G|WF|IWF|H|BK)\d'

# ---------------------------------------------------------------------------
# Beam table (optional): parses a region the user windows around a
# schedule table into {label: (b_mm, h_mm)} -- see ab_shapes.
# parse_beam_table. Biggest risk in this tool; report the parsed table
# back for verification before using it (never silently trust an OCR-
# like grid parse), and keep the report grid EDITABLE so a bad/missing
# cell can be hand-corrected rather than blocking the whole table.
# ---------------------------------------------------------------------------

# Text within this distance (mm) of each other's Y coordinate is
# treated as the same table ROW; same idea for X and COLUMNS. A real
# table's row/column pitch is comfortably larger than typical text
# height/CAD tracing jitter, so this only needs to be "small enough to
# not merge two real rows/columns", not tuned per drawing.
TABLE_ROW_CLUSTER_TOL_MM = 30.0
TABLE_COL_CLUSTER_TOL_MM = 30.0

# Header row/column identification (case-insensitive substring match
# against the header cell's own text). The label column additionally
# falls back to "whichever column is NOT identified as b or h" when no
# header hint matches it, since many tables just say "TIPE"/"NO" or
# nothing distinctive at all for that column.
TABLE_LABEL_HEADER_HINTS = ["TIPE", "TYPE", "LABEL", "NAMA", "NO"]
TABLE_B_HEADER_HINTS = ["B", "LEBAR", "WIDTH"]
TABLE_H_HEADER_HINTS = ["H", "TINGGI", "HEIGHT", "DEPTH"]

# ---------------------------------------------------------------------------
# Sizing fallback: h has no CAD source in 2-line mode (only b comes from
# the drawn edge gap) and no source at all in 1-line mode. Rather than
# leaving h blank and blocking Generate on every row until it's typed
# by hand, resolve_type_sizing() prefills this default -- CLEARLY
# marked in the Size source column so it never looks like real data --
# and it's fully editable in the BEAM TYPES grid before Generate.
# A beam table match always wins over this default (see
# resolve_type_sizing in ab_shapes.py); set to None to go back to
# leaving h blank/manual instead.
# ---------------------------------------------------------------------------

DEFAULT_BEAM_HEIGHT_MM = 500.0

# ---------------------------------------------------------------------------
# Generate (M7): concrete rectangular beam family
# ---------------------------------------------------------------------------

RECT_CONCRETE_BEAM_FAMILY_NAME_CANDIDATES = ["M_Concrete-Rectangular Beam", "Concrete-Rectangular Beam"]
RECT_CONCRETE_BEAM_FAMILY_FILE_CANDIDATES = ["M_Concrete-Rectangular Beam.rfa", "Concrete-Rectangular Beam.rfa"]

CONCRETE_BEAM_LIBRARY_ROOTS = [
    r"C:\ProgramData\Autodesk\RVT 2026\Libraries\English\US\Structural Framing\Concrete",
    r"C:\ProgramData\Autodesk\RVT 2025\Libraries\English\US\Structural Framing\Concrete",
    r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Framing\Concrete",
]

# Real parameter names confirmed at runtime (see ab_kinds.py), not
# assumed -- these are just the candidates to check.
BEAM_WIDTH_PARAM_CANDIDATES = ["b", "Width"]
BEAM_DEPTH_PARAM_CANDIDATES = ["h", "Height", "Depth"]

BEAM_MATERIAL_PARAM_CANDIDATES = ["Structural Material", "Material"]
CONCRETE_MATERIAL_NAME_CANDIDATES = [
    "Concrete - Cast-in-Place Concrete",
    "Concrete, Cast-in-Place gray",
    "Concrete, Cast-in-Place",
    "Concrete",
]

# ---------------------------------------------------------------------------
# Generate (M7): steel beam families + section snapping
# ---------------------------------------------------------------------------

# Same "no SNI library on this machine" finding as Auto Column -- falls
# back to AISC throughout (ab_config.BEAM_STEEL_LIBRARY_ROOTS). Framing
# family files/catalogs use plain names (no "-Column" suffix) --
# confirm at runtime (ab_sections.py) rather than assume.
_BEAM_STEEL_ROOT_2025 = r"C:\ProgramData\Autodesk\RVT 2025\Libraries\English\US\Structural Framing\Steel"
_BEAM_STEEL_ROOT_2026 = r"C:\ProgramData\Autodesk\RVT 2026\Libraries\English\US\Structural Framing\Steel"
BEAM_STEEL_LIBRARY_ROOTS = [
    _BEAM_STEEL_ROOT_2026 + r"\AISC 15.0",
    _BEAM_STEEL_ROOT_2025 + r"\AISC 15.0",
    _BEAM_STEEL_ROOT_2026 + r"\AISC 14.1",
    _BEAM_STEEL_ROOT_2025 + r"\AISC 14.1",
    _BEAM_STEEL_ROOT_2026,
    _BEAM_STEEL_ROOT_2025,
]

W_SHAPE_BEAM_CATALOG_FILE_CANDIDATES = ["M_W Shapes.txt", "M_W-Wide Flange.txt"]
W_SHAPE_BEAM_FAMILY_FILE_CANDIDATES = ["M_W Shapes.rfa", "M_W-Wide Flange.rfa"]

C_SHAPE_BEAM_CATALOG_FILE_CANDIDATES = ["M_C Shapes.txt", "M_C-Channel.txt"]
C_SHAPE_BEAM_FAMILY_FILE_CANDIDATES = ["M_C Shapes.rfa", "M_C-Channel.rfa"]

HSS_RECT_BEAM_CATALOG_FILE_CANDIDATES = ["M_HSS Rectangular.txt", "M_HSS-Hollow Structural Section.txt"]
HSS_RECT_BEAM_FAMILY_FILE_CANDIDATES = ["M_HSS Rectangular.rfa", "M_HSS-Hollow Structural Section.rfa"]

# A label starting with one of these (checked before the plain WF/H
# patterns in STEEL_LABEL_PATTERNS above) identifies a channel or HSS
# profile instead of the default wide-flange assumption -- there is no
# shape signature to fall back on here (plan geometry is just lines),
# so the label prefix is the ONLY cue available for which catalog to
# snap against. Genuinely editable: add local office conventions here.
STEEL_CHANNEL_LABEL_PATTERNS = [r'^C\d+[Xx]']       # C150X50, C100x50 (channel dimension format)
STEEL_HSS_LABEL_PATTERNS = [r'^HSS', r'^BOX']

# ---------------------------------------------------------------------------
# Generate (M9): timber beam family + section snapping
# ---------------------------------------------------------------------------

# Revit's own default timber framing family ships as a type catalog too
# (like the steel families above), NOT a parametric b/h family --
# confirmed at runtime by reading M_Timber.txt: one row per standard
# sawn/glulam size, dimensions given in INCHES (b/d headers), unlike
# the steel catalogs' millimetre columns -- ab_sections.py converts.
TIMBER_LIBRARY_ROOTS = [
    r"C:\ProgramData\Autodesk\RVT 2026\Libraries\English\US\Structural Framing\Wood",
    r"C:\ProgramData\Autodesk\RVT 2025\Libraries\English\US\Structural Framing\Wood",
    r"C:\ProgramData\Autodesk\RVT 2024\Libraries\English\US\Structural Framing\Wood",
]
TIMBER_FAMILY_FILE_CANDIDATES = ["M_Timber.rfa"]
TIMBER_CATALOG_FILE_CANDIDATES = ["M_Timber.txt"]
TIMBER_CATALOG_UNIT_SCALE_TO_MM = 25.4  # inches -> mm

# ---------------------------------------------------------------------------
# Generate (M7): vertical placement
# ---------------------------------------------------------------------------

# TOP of beam flush with the selected level (Z Justification = Top,
# confirmed at runtime -- see ab_kinds.py); no offset knob exposed yet.
BEAM_Z_OFFSET_FT = 0.0

# ---------------------------------------------------------------------------
# Auto-join (M8, after Generate)
# ---------------------------------------------------------------------------

# Confirmed hierarchy per the spec: FLOOR > COLUMN > BEAM. Floor cuts a
# concrete beam; column cuts a concrete beam (the exact reciprocal of
# Auto Column's own rule, where the column cuts framing). Both always
# run for concrete -- explicitly specified. Steel/timber beam join, and
# beam-to-beam join, were NOT specified/confirmed (no steel/timber CAD
# data was available to validate against, same caveat as Auto Column's
# ENABLE_STEEL_COLUMN_JOIN) -- off by default; flip to True once confirmed.
ENABLE_STEEL_BEAM_JOIN = False
ENABLE_TIMBER_BEAM_JOIN = False
ENABLE_BEAM_BEAM_JOIN = False

# Bounding-box expansion (mm) used to spatially prefilter candidate
# columns/floors near each placed beam, before attempting a real join.
JOIN_SEARCH_MARGIN_MM = 300.0
