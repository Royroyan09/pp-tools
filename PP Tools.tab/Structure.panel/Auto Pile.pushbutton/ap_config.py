# -*- coding: utf-8 -*-
"""Auto Pile configuration.

Shared tunables (unit conversion, CAD tolerances, shape classification,
label matching, placement) live in pp_common.config_base and are
inherited here; only Pile-specific detection rules are defined below.
"""
from pp_common.config_base import *

# ---------------------------------------------------------------------------
# Shape classification
# ---------------------------------------------------------------------------

# A closed polyline/block outline within this tolerance (mm) of
# Width == Length counts as a square pile (Side shown in the type
# list); larger differences keep separate Width/Length.
SQUARE_SIDE_TOLERANCE_MM = 20.0

# Block names that clearly indicate shape (checked case-insensitively,
# substring match), used ONLY when block names are recovered via the
# DXF-export fallback (pp_common.dxf_text) — Revit's geometry API
# exposes no name for a DWG block at all (GeometryInstance has no
# Symbol/name property; its SymbolGeometryId resolves back to the
# whole CADLinkType, not the individual block — verified against a
# real model). Recursing into the block's own nested geometry to find
# a circle vs. a rectangle is the primary, always-available detection
# method; these hints are a secondary refinement.
BLOCK_NAME_CIRCLE_HINTS = ["circle", "round", "bore", "bulat"]
BLOCK_NAME_SQUARE_HINTS = ["square", "rect", "persegi", "kotak"]

# ---------------------------------------------------------------------------
# Layer-name hints (used only to pre-select combo boxes; picking in the
# drawing always wins). Extend with local naming conventions.
# ---------------------------------------------------------------------------

PERIMETER_LAYER_HINTS = ["PILE", "BORE", "TIANG", "PANCANG", "PCAP"]
LABEL_LAYER_HINTS = ["LABEL", "TEXT", "NOTASI", "KETERANGAN", "MARK"]

# ---------------------------------------------------------------------------
# Pile families (category OST_StructuralFoundation, material Concrete)
# ---------------------------------------------------------------------------

# Round piles: family names accepted as the base circular pile family,
# checked case-insensitively against families already loaded. Verified
# against a real Revit 2025/2026 library: "M_Pile_Beton" ("beton" is
# Indonesian for concrete) is a point-based Structural Foundation family
# with writable Diameter/Depth type parameters.
ROUND_FAMILY_NAME_CANDIDATES = [
    "M_Pile_Beton",
    "Pile_Beton",
    "M_Pile-Round-Concrete",
    "Pile-Round-Concrete",
]
ROUND_FAMILY_FILE_CANDIDATES = ["M_Pile_Beton.rfa", "Pile_Beton.rfa"]
ROUND_FAMILY_LIBRARY_ROOTS = [
    r"C:\ProgramData\Autodesk\RVT 2026\Libraries",
    r"C:\ProgramData\Autodesk\RVT 2025\Libraries",
    r"C:\ProgramData\Autodesk\RVT 2024\Libraries",
]
DIAMETER_PARAM_CANDIDATES = ["Diameter", "d", "Pile Diameter"]

# Square/rectangular piles: no concrete family of this shape exists in
# Revit's standard library (only a square STEEL pipe pile) -- verified
# by searching every Structural Foundations folder in the installed
# 2025/2026 libraries. PP_Pile-Square-Concrete.rfa is a minimal family
# authored for this project (Family Editor API, from the stock "Metric
# Structural Foundation.rft" template) and ships bundled with this
# pushbutton rather than a system library path.
SQUARE_FAMILY_NAME_CANDIDATES = ["PP_Pile-Square-Concrete"]
SQUARE_FAMILY_FILE_CANDIDATES = ["PP_Pile-Square-Concrete.rfa"]

WIDTH_PARAM_CANDIDATES = ["Width", "b"]
LENGTH_PARAM_CANDIDATES = ["Length", "l"]

# The vertical/depth parameter's real name is NOT assumed -- it is
# confirmed at runtime per family, since it varies (M_Pile_Beton uses
# "Depth"; the bundled square family reuses the Structural Foundation
# template's "Foundation Thickness").
DEPTH_PARAM_CANDIDATES = ["Depth", "Foundation Thickness", "Length", "Pile Length"]

# Some pile families (M_Pile_Beton confirmed) add a "Minimum Embedment"
# on top of Depth, so the physical pile ends up longer than the Depth
# value entered -- zeroed out on every generated type so Depth alone is
# the pile's exact total length, matching "extends downward by Depth."
# Optional: only set when the resolved family actually has one of these.
EMBEDMENT_PARAM_CANDIDATES = ["Minimum Embedment", "Embedment"]

# Structural material: optional -- M_Pile_Beton has NO settable material
# parameter at all (verified live), so material is only set when the
# resolved family actually exposes one (e.g. the bundled square family's
# "Structural Material"). Concrete material name candidates, checked
# case-insensitively; the first one found/created in the document wins.
MATERIAL_PARAM_CANDIDATES = ["Structural Material", "Material"]
CONCRETE_MATERIAL_NAME_CANDIDATES = [
    "Concrete - Cast-in-Place Concrete",
    "Concrete, Cast-in-Place gray",
    "Concrete, Cast-in-Place",
    "Concrete",
]
