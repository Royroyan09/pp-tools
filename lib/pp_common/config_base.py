# -*- coding: utf-8 -*-
"""Default tunables shared by every auto-modelling tool's config module.

A tool's own af_config.py / ap_config.py does:

    from pp_common.config_base import *

and then only defines/overrides values specific to that tool (family
and parameter name candidates, thickness/depth maps, layer hints...).
Keeping the shared defaults in one place means a tolerance fix benefits
every tool at once, while each tool can still override a single value
without touching this file.
"""

MM_TO_FT = 1.0 / 304.8

# ---------------------------------------------------------------------------
# CAD reading tolerances
# ---------------------------------------------------------------------------

# Endpoint tolerance used when chaining loose line segments into closed
# loops (exploded rectangles are common in DWGs).
CHAIN_TOLERANCE_MM = 5.0

# Outlines whose bounding box falls outside this range are treated as
# noise (text strokes, symbols) or as unrelated geometry and skipped.
MIN_FOOTPRINT_SIDE_MM = 200.0
MAX_FOOTPRINT_SIDE_MM = 20000.0

# Same-label shapes are expected to share identical dimensions; larger
# differences are reported (largest size wins).
SIZE_MISMATCH_TOLERANCE_MM = 10.0

# ---------------------------------------------------------------------------
# Shape classification
# ---------------------------------------------------------------------------

# An outline counts as a clean rectangle/square when its area fills at
# least this fraction of its oriented (min-area) bounding box; a
# triangle is ~0.5, an L-shape is often ~0.6.
RECT_AREA_RATIO = 0.95

# Rotation angles within this many degrees of 0/90 snap to the axis, so
# clean orthogonal drawings don't produce microscopically rotated
# instances.
ROTATION_SNAP_DEG = 0.5

# ---------------------------------------------------------------------------
# Label matching
# ---------------------------------------------------------------------------

# Optional regex a CAD text must match to count as a label (None =
# accept any non-empty text on the label layer).
LABEL_REGEX = None

LABEL_UPPERCASE = True

# Max distance (mm) from a shape's edge for the pairing fallback when a
# label's insertion point is not inside any shape. Keeps texts in
# legend tables / title blocks from being matched (None = no limit).
LABEL_MAX_DISTANCE_MM = 15000.0

# Shapes that end up with no label are grouped by (rounded) size and
# named with this format.
UNMATCHED_LABEL_FORMAT = u"X{n}"
SIZE_GROUP_ROUND_MM = 10.0

# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

# The top of the generated element must sit flush with the selected
# level; after placement the instance offset is corrected if the top
# face deviates by more than this (feet).
TOP_FLUSH_TOLERANCE_FT = 0.001

# Format for the duplicated type name; {label} is the CAD label.
TYPE_NAME_FORMAT = u"{label}"

# ---------------------------------------------------------------------------
# Dimension cleanup
# ---------------------------------------------------------------------------

# A measured CAD dimension this close (mm) to a clean multiple of
# DIMENSION_SNAP_MM snaps to it (tracing/OCR imprecision — e.g. a footing
# measuring 1989.99mm is obviously meant to be 2000mm). A dimension
# further away than the tolerance is left exactly as measured rather
# than forced to the nearest step -- never guess a genuinely custom
# size. Set DIMENSION_SNAP_TOLERANCE_MM to 0 to disable.
#
# Kept meaningfully below half of DIMENSION_SNAP_MM (25mm here) on
# purpose: a tolerance at or above half the snap step would round EVERY
# dimension unconditionally (the nearest multiple is never more than
# half a step away), silently defeating the "never guess" gate this is
# supposed to be.
DIMENSION_SNAP_MM = 50.0
DIMENSION_SNAP_TOLERANCE_MM = 20.0

# ---------------------------------------------------------------------------
# Open-line geometry (pp_common.geometry.Segment / pair_parallel_lines /
# is_collinear) -- added for Auto Beam's centerline/curve path, the
# line-based counterpart to Footprint's point-placed shapes.
# ---------------------------------------------------------------------------

# Two edge lines pair into one beam centerline (2-line mode) when they
# run parallel within this many degrees...
LINE_PARALLEL_TOL_DEG = 3.0
# ...within this perpendicular gap (mm) of each other (the beam's own
# width b) -- wide enough for a real beam, tight enough to not pair two
# unrelated nearby lines...
LINE_PAIR_MAX_GAP_MM = 1000.0
# ...and overlap along their shared direction by at least this much
# (mm) -- rules out two lines that are merely parallel by coincidence
# with little to no real shared span.
LINE_PAIR_MIN_OVERLAP_MM = 200.0

# Two centerline segments count as lying on the same infinite line
# (collinear -- a candidate to stitch into one continuous beam) when
# within this many degrees of each other...
COLLINEAR_ANGLE_TOL_DEG = 2.0
# ...and within this perpendicular offset (mm) of each other's line.
COLLINEAR_OFFSET_TOL_MM = 50.0
