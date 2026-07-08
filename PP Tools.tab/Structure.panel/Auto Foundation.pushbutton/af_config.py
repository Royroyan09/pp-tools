# -*- coding: utf-8 -*-
"""Auto Foundation configuration.

Shared tunables (unit conversion, CAD tolerances, shape classification,
label matching, placement) live in pp_common.config_base and are
inherited here; only Foundation-specific detection rules and naming
conventions are defined below, so local standards (e.g. Indonesian
layer names / default thickness tables) can be plugged in without
touching the tool logic.
"""
from pp_common.config_base import *

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

# ---------------------------------------------------------------------------
# Non-rectangular pads (triangle / L-shape / custom outline)
# ---------------------------------------------------------------------------

# Generated as structural foundation slabs. Preferred type-name fragments
# (case-insensitive); if none match, the first foundation slab type found
# in the project is used as the base for duplication.
FOUNDATION_SLAB_TYPE_CANDIDATES = ["Foundation Slab", "Fondasi"]

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Default thickness per label (mm), pre-filled in the type list.
# Plug Indonesian-standard values in here later, e.g. {"F1": 300.0}.
DEFAULT_THICKNESS_MM = {}

# ---------------------------------------------------------------------------
# Layer-name hints (used only to pre-select combo boxes; picking in the
# drawing always wins). Extend with local naming conventions.
# ---------------------------------------------------------------------------

PERIMETER_LAYER_HINTS = ["FOUND", "FOOT", "PONDASI", "TAPAK", "PILE"]
LABEL_LAYER_HINTS = ["LABEL", "TEXT", "NOTASI", "KETERANGAN", "MARK"]
