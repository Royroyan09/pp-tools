# -*- coding: utf-8 -*-
"""Auto Foundation configuration.

All detection rules, tolerances and naming conventions live here so
that local standards (e.g. Indonesian layer names / default thickness
tables) can be plugged in later without touching the tool logic.
"""

MM_TO_FT = 1.0 / 304.8

# ---------------------------------------------------------------------------
# Foundation family (rectangular isolated pad footing)
# ---------------------------------------------------------------------------

# Family names accepted as the base rectangular footing family, checked
# case-insensitively against families already loaded in the project.
FAMILY_NAME_CANDIDATES = [
    "Footing-Rectangular",
    "M_Footing-Rectangular",
    "Footing-Rectangular_M",
]

# File names to look for when the family is not loaded yet.
FAMILY_FILE_CANDIDATES = [
    "M_Footing-Rectangular.rfa",
    "Footing-Rectangular.rfa",
]

# Library roots searched (recursively) for the family files above.
FAMILY_LIBRARY_ROOTS = [
    r"C:\ProgramData\Autodesk\RVT 2026\Libraries",
    r"C:\ProgramData\Autodesk\RVT 2025\Libraries",
    r"C:\ProgramData\Autodesk\RVT 2024\Libraries",
]

# Type parameter names, checked case-insensitively against the symbol's
# writable length parameters. Names vary between family versions, which
# is why they are resolved at runtime instead of hard-coded.
WIDTH_PARAM_CANDIDATES = ["Width", "b", "Foundation Width"]
LENGTH_PARAM_CANDIDATES = ["Length", "l", "Foundation Length"]
THICKNESS_PARAM_CANDIDATES = ["Foundation Thickness", "Thickness", "h"]

# Format for the duplicated type name; {label} is the CAD label (F1, F2...).
TYPE_NAME_FORMAT = u"{label}"

# ---------------------------------------------------------------------------
# Non-rectangular pads (triangle / L-shape / custom outline)
# ---------------------------------------------------------------------------

# Generated as structural foundation slabs. Preferred type-name fragments
# (case-insensitive); if none match, the first foundation slab type found
# in the project is used as the base for duplication.
FOUNDATION_SLAB_TYPE_CANDIDATES = ["Foundation Slab", "Fondasi"]

# A footprint counts as rectangular when its area fills at least this
# fraction of its oriented (min-area) bounding box; triangles are ~0.5.
RECT_AREA_RATIO = 0.95

# Rotation angles within this many degrees of 0/90 snap to the axis, so
# clean orthogonal drawings don't produce microscopically rotated footings.
ROTATION_SNAP_DEG = 0.5

# ---------------------------------------------------------------------------
# CAD reading tolerances / filters
# ---------------------------------------------------------------------------

# Endpoint tolerance used when chaining loose line segments into closed
# loops (exploded rectangles are common in DWGs).
CHAIN_TOLERANCE_MM = 5.0

# Footprints whose bounding box falls outside this range are treated as
# noise (text strokes, symbols) or as non-pad outlines and skipped.
MIN_FOOTPRINT_SIDE_MM = 200.0
MAX_FOOTPRINT_SIDE_MM = 20000.0

# Same-label footings are expected to share identical W x L; differences
# above this tolerance are reported (largest size wins).
SIZE_MISMATCH_TOLERANCE_MM = 10.0

# ---------------------------------------------------------------------------
# Label matching
# ---------------------------------------------------------------------------

# Optional regex a CAD text must match to count as a foundation label
# (None = accept any non-empty text on the label layer).
# Example for Indonesian drawings: r'^(F|P|TP)\d+$'
LABEL_REGEX = None

LABEL_UPPERCASE = True

# Max distance (mm) for the nearest-centroid fallback when a label's
# insertion point is not inside any perimeter (None = no limit).
LABEL_MAX_DISTANCE_MM = None

# Footprints that end up with no label are grouped by (rounded) size and
# named with this format.
UNMATCHED_LABEL_FORMAT = u"X{n}"
SIZE_GROUP_ROUND_MM = 10.0

# ---------------------------------------------------------------------------
# Defaults / placement
# ---------------------------------------------------------------------------

# Default thickness per label (mm), pre-filled in the type list.
# Plug Indonesian-standard values in here later, e.g. {"F1": 300.0}.
DEFAULT_THICKNESS_MM = {}

# The top of the footing must sit flush with the selected level; after
# placement the instance offset is corrected if the top face deviates by
# more than this (feet).
TOP_FLUSH_TOLERANCE_FT = 0.001

# ---------------------------------------------------------------------------
# Layer-name hints (used only to pre-select combo boxes; picking in the
# drawing always wins). Extend with local naming conventions.
# ---------------------------------------------------------------------------

PERIMETER_LAYER_HINTS = ["FOUND", "FOOT", "PONDASI", "TAPAK", "PILE"]
LABEL_LAYER_HINTS = ["LABEL", "TEXT", "NOTASI", "KETERANGAN", "MARK"]
