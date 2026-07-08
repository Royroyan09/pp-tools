# -*- coding: utf-8 -*-
"""CAD text reader shared by every auto-modelling tool.

Revit's geometry API does not expose text entities from imported or
linked DWGs at all (there is no text geometry class in current API
versions — verified live on Revit 2026: an import whose drawing holds
38 labels returns zero text objects). The text does, however, survive
a round-trip through Revit's own DXF export.

So: export the active view to DXF in a temp folder, parse the DXF
(plain text) for TEXT/MTEXT entities — resolving nested BLOCK/INSERT
transforms, since Revit exports each import and xref as nested blocks —
and hand back cleaned values with model coordinates in feet.
"""
import io
import math
import os
import re

from pp_common.config_base import MM_TO_FT

_EXPORT_NAME = "pp_common_texts"


# ---------------------------------------------------------------------------
# Revit-side export
# ---------------------------------------------------------------------------

def read_cad_texts(doc, layer_name):
    """Exports the active view to DXF and returns [(x_ft, y_ft, value)]
    for text entities whose layer matches layer_name (tolerant match;
    falls back to all texts when nothing matches). Raises on export
    failure — callers treat that as 'no texts available'."""
    folder = _export_active_view(doc)
    path = os.path.join(folder, _EXPORT_NAME + ".dxf")
    texts = parse_dxf_texts(path)
    filtered = [t for t in texts if _layer_matches(t[0], layer_name)]
    if not filtered:
        filtered = texts
    return [(x * MM_TO_FT, y * MM_TO_FT, v) for (_layer, x, y, v) in filtered]


def _export_active_view(doc):
    import clr
    clr.AddReference('RevitAPI')
    from Autodesk.Revit.DB import DXFExportOptions, ElementId
    from System.Collections.Generic import List as NetList
    from System.IO import Path

    folder = os.path.join(Path.GetTempPath(), "pp_autofoundation")
    if not os.path.isdir(folder):
        os.makedirs(folder)
    for ext in (".dxf", ".pcp"):
        stale = os.path.join(folder, _EXPORT_NAME + ext)
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except Exception:
                pass

    opts = DXFExportOptions()
    try:
        from Autodesk.Revit.DB import ExportUnit
        opts.TargetUnit = ExportUnit.Millimeter
    except Exception:
        pass
    try:
        # export around the project internal origin so DXF coordinates
        # line up with the model coordinates the shapes were read in
        opts.SharedCoords = False
    except Exception:
        pass

    ids = NetList[ElementId]()
    ids.Add(doc.ActiveView.Id)
    if not doc.Export(folder, _EXPORT_NAME, ids, opts):
        raise Exception("Revit refused to export the active view to DXF.")
    return folder


def _layer_matches(dxf_layer, wanted):
    """DWG xref layers surface as '<xref>$0$<layer>'; the text often
    lives on a sibling layer of the one the user picked (leader lines
    vs. text), so same-xref counts as a match."""
    if not wanted:
        return True
    if dxf_layer == wanted:
        return True
    if "$0$" in dxf_layer and "$0$" in wanted:
        return dxf_layer.split("$0$", 1)[0] == wanted.split("$0$", 1)[0]
    return False


# ---------------------------------------------------------------------------
# DXF parsing (pure Python, no Revit API — unit-testable offline)
# ---------------------------------------------------------------------------

def _clean_text(value):
    """Strips MTEXT inline formatting: {\\fArial|b0;P3} -> P3."""
    value = re.sub(r'\\[A-Za-z][^;\\{}]*;', '', value)
    value = value.replace('{', '').replace('}', '')
    value = value.replace('\\P', ' ').replace('\\~', ' ')
    return value.strip()


def _read_pairs(path):
    with io.open(path, encoding='utf-8', errors='replace') as f:
        lines = [l.rstrip('\r\n') for l in f]
    return [(lines[i].strip(), lines[i + 1])
            for i in range(0, len(lines) - 1, 2)]


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + b * y + e, c * x + d * y + f)


def _compose(p, q):
    """p after q: total(x) = p(q(x))."""
    pa, pb, pc, pd, pe, pf = p
    qa, qb, qc, qd, qe, qf = q
    return (pa * qa + pb * qc, pa * qb + pb * qd,
            pc * qa + pd * qc, pc * qb + pd * qd,
            pa * qe + pb * qf + pe,
            pc * qe + pd * qf + pf)


def _insert_matrix(ix, iy, sx, sy, rot_deg, base):
    """Block-local point -> parent space: T + R*S*(p - base)."""
    r = math.radians(rot_deg)
    cr, sr = math.cos(r), math.sin(r)
    a, b = cr * sx, -sr * sy
    c, d = sr * sx, cr * sy
    bx, by = base
    return (a, b, c, d,
            ix - (a * bx + b * by),
            iy - (c * bx + d * by))


def parse_dxf_texts(path):
    """Returns [(layer, x, y, value)] in DXF drawing units, with nested
    BLOCK/INSERT transforms resolved down to model space."""
    pairs = _read_pairs(path)
    n = len(pairs)

    # block name -> {'base': (x, y), 'texts': [...], 'inserts': [...]}
    blocks = {}
    model = {'texts': [], 'inserts': []}

    section = None
    block = None
    i = 0
    while i < n:
        code, raw = pairs[i]
        val = raw.strip()
        if code == '0' and val == 'SECTION' and i + 1 < n and pairs[i + 1][0] == '2':
            section = pairs[i + 1][1].strip()
        elif code == '0' and val == 'ENDSEC':
            section = None
        elif code == '0' and val == 'BLOCK':
            j = i + 1
            name = None
            bx = by = 0.0
            while j < n and pairs[j][0] != '0':
                c, d = pairs[j]
                if c == '2':
                    name = d.strip()
                elif c == '10':
                    bx = float(d)
                elif c == '20':
                    by = float(d)
                j += 1
            block = name
            blocks[block] = {'base': (bx, by), 'texts': [], 'inserts': []}
            i = j
            continue
        elif code == '0' and val == 'ENDBLK':
            block = None
        elif code == '0' and val in ('TEXT', 'MTEXT'):
            j = i + 1
            layer = ''
            x = y = 0.0
            txt = ''
            while j < n and pairs[j][0] != '0':
                c, d = pairs[j]
                if c == '8':
                    layer = d.strip()
                elif c == '10':
                    x = float(d)
                elif c == '20':
                    y = float(d)
                elif c in ('1', '3'):
                    txt += d
                j += 1
            rec = (layer, x, y, txt)
            (blocks[block]['texts'] if block is not None else model['texts']).append(rec)
            i = j
            continue
        elif code == '0' and val == 'INSERT':
            j = i + 1
            name = ''
            x = y = 0.0
            sx = sy = 1.0
            rot = 0.0
            while j < n and pairs[j][0] != '0':
                c, d = pairs[j]
                if c == '2':
                    name = d.strip()
                elif c == '10':
                    x = float(d)
                elif c == '20':
                    y = float(d)
                elif c == '41':
                    sx = float(d)
                elif c == '42':
                    sy = float(d)
                elif c == '50':
                    rot = float(d)
                j += 1
            rec = (name, x, y, sx, sy, rot)
            (blocks[block]['inserts'] if block is not None else model['inserts']).append(rec)
            i = j
            continue
        i += 1

    out = []

    def emit(block_name, matrix, depth):
        if depth > 12 or block_name not in blocks:
            return
        b = blocks[block_name]
        for layer, x, y, txt in b['texts']:
            px, py = _apply(matrix, x, y)
            value = _clean_text(txt)
            if value:
                out.append((layer, px, py, value))
        for name, ix, iy, sx, sy, rot in b['inserts']:
            m = _compose(matrix, _insert_matrix(
                ix, iy, sx, sy, rot, blocks.get(name, {'base': (0.0, 0.0)})['base']))
            emit(name, m, depth + 1)

    for layer, x, y, txt in model['texts']:
        value = _clean_text(txt)
        if value:
            out.append((layer, x, y, value))
    for name, ix, iy, sx, sy, rot in model['inserts']:
        m = _insert_matrix(ix, iy, sx, sy, rot,
                           blocks.get(name, {'base': (0.0, 0.0)})['base'])
        emit(name, m, 1)

    return out
