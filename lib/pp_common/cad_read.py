# -*- coding: utf-8 -*-
"""CAD import/layer reading shared by every auto-modelling tool:
listing imports and their layers, walking an ImportInstance's geometry
bucketed by target GraphicsStyleCategory, and the PickObject mechanics
used by "Pick Layer" / "Pick Label" style buttons.

Kept free of forms.alert()-style UX: callers decide how to surface
failures (title text differs per tool), this module only raises
ValueError with a short machine-checkable reason or returns None.
"""
import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

from Autodesk.Revit.DB import (
    FilteredElementCollector, ImportInstance, GeometryInstance, PolyLine,
    Line, Arc, ElementId
)
from Autodesk.Revit.UI.Selection import ObjectType

from pp_common.units import get_name


def try_get_text_class():
    """Newer Revit API versions expose CAD text as Autodesk.Revit.DB.Text
    geometry objects with a readable .Value string. Older versions (and,
    empirically, imported/linked DWG text on Revit 2026) don't expose any
    text geometry at all, so the import itself must be attempted lazily
    (inside a function, wrapped in try/except) rather than at module load
    time. Callers should treat a return of None as "use the DXF fallback"
    (pp_common.dxf_text)."""
    try:
        from Autodesk.Revit.DB import Text as DBText
        return DBText
    except Exception:
        return None


def list_cad_imports(doc):
    """Returns [(display_name, ImportInstance)]."""
    imports = FilteredElementCollector(doc).OfClass(ImportInstance).ToElements()
    result = []
    for imp in imports:
        cat = imp.Category
        name = cat.Name if cat is not None else get_name(imp)
        if not name:
            name = "CAD Import"
        result.append((name, imp))
    return result


def list_layers(import_instance):
    """Returns the import's layers (Category.SubCategories) sorted by
    name."""
    cat = import_instance.Category
    layers = []
    if cat is not None:
        try:
            for sub in cat.SubCategories:
                layers.append(sub)
        except Exception:
            pass
    layers.sort(key=lambda c: c.Name)
    return layers


def _as_id_set(target):
    """Normalizes a 'which layer(s)' argument down to a set of
    ElementId. Accepts a single ElementId (the original, still-
    supported call shape), a single Category (has .Id, e.g. straight
    from cad_read.list_layers), a pp_common.wpf_helpers.LayerSelection
    (has .ids), or a plain iterable of any of those -- lets a "layer"
    picked via pick_points_on_cad_multi/resolve_cad_layers actually
    span several real CAD layers (common on multi-block-flattened DWG
    imports where the same logical beam/label/perimeter layer got
    split into near-duplicate sub-layers) without every existing
    single-layer call site needing to change."""
    if isinstance(target, ElementId):
        return set([target])
    if hasattr(target, 'ids'):
        return set(target.ids)
    if hasattr(target, 'Id'):
        return set([target.Id])
    ids = set()
    for t in target:
        ids |= _as_id_set(t)
    return ids


def collect_layer_geometry(doc, geom_element, target_cat_ids, DBText=None):
    """Walks an ImportInstance's geometry (recursing into
    GeometryInstance) and buckets Line/PolyLine/Text objects by which of
    the given target categories they belong to.

    target_cat_ids: {alias: <layer(s)>} — lets a caller scan several
    logical layers (e.g. 'perim' and 'label') in a single walk; each
    value is anything _as_id_set understands (a single ElementId/
    Category, a LayerSelection, or an iterable of them), so one alias
    can itself span multiple real CAD layers.
    Returns {alias: {'polylines': [[(x,y),...]], 'lines': [((x,y),(x,y))],
    'texts': [(XYZ, str)]}}.
    """
    result = {alias: {'polylines': [], 'lines': [], 'texts': []}
             for alias in target_cat_ids}
    target_id_sets = {alias: _as_id_set(target)
                      for alias, target in target_cat_ids.items()}

    def walk(g):
        if g is None:
            return
        for obj in g:
            if isinstance(obj, GeometryInstance):
                try:
                    walk(obj.GetInstanceGeometry())
                except Exception:
                    pass
                continue

            try:
                style_id = obj.GraphicsStyleId
            except Exception:
                style_id = None
            if style_id is None or style_id == ElementId.InvalidElementId:
                continue
            gs = doc.GetElement(style_id)
            if gs is None or gs.GraphicsStyleCategory is None:
                continue
            cat_id = gs.GraphicsStyleCategory.Id

            for alias, wanted_ids in target_id_sets.items():
                if cat_id not in wanted_ids:
                    continue
                bucket = result[alias]
                if isinstance(obj, PolyLine):
                    try:
                        bucket['polylines'].append(
                            [(c.X, c.Y) for c in obj.GetCoordinates()])
                    except Exception:
                        pass
                elif isinstance(obj, Line):
                    p0, p1 = obj.GetEndPoint(0), obj.GetEndPoint(1)
                    bucket['lines'].append(((p0.X, p0.Y), (p1.X, p1.Y)))
                elif DBText is not None and isinstance(obj, DBText):
                    try:
                        bucket['texts'].append((obj.Position, obj.Value))
                    except Exception:
                        pass

    walk(geom_element)
    return result


def collect_shape_entities(doc, geom_element, target_cat_id,
                          include_bound_arc_chords=False):
    """Walks an ImportInstance's geometry looking for cross-section
    candidates (piles, columns, ...) on one logical target layer: true
    CAD circles (unbound Arcs), closed polyline/loose-line outlines,
    and block references. target_cat_id is anything _as_id_set
    understands -- a single ElementId/Category (the original call
    shape) or a LayerSelection/iterable spanning several real CAD
    layers.

    A block reference is a GeometryInstance whose OWN GraphicsStyleId
    is one of target_cat_id's layers — i.e. the layer the INSERT
    itself sits on.
    Its nested geometry is handed back unresolved (not auto-walked into
    circles/polylines here) so the caller can classify its shape:
    Revit's geometry API exposes no name for a DWG block (GeometryInstance
    has no Symbol/name property; its SymbolGeometryId resolves back to
    the whole CADLinkType, not the individual block — verified against a
    real model), so recursing into the nested geometry to look for a
    circle vs. a rectangle is the only reliable detection method.

    Geometry belonging to OTHER layers is still recursed into
    transparently (entities keep their own layer inside a block, per
    normal DWG semantics) — only a GeometryInstance whose OWN layer
    matches target_cat_id is treated as a block and left unresolved.

    include_bound_arc_chords: when True, BOUND arcs (corner fillets on
    steel profiles, rounded outline corners) contribute their chord as a
    line segment so loop-chaining can close outlines that run through
    them. Off by default: bound arcs are usually symbols/hatching noise
    on pile/footing layers, and Auto Pile's behaviour predates (and was
    verified without) this flag.

    Returns {'circles': [(cx, cy, radius_ft)], 'polylines': [[(x,y),...]],
    'lines': [((x,y),(x,y))], 'blocks': [{'position': (x,y),
    'geometry': GeometryElement-or-None}]}.
    """
    result = {'circles': [], 'polylines': [], 'lines': [], 'blocks': []}
    wanted_ids = _as_id_set(target_cat_id)

    def own_layer_id(obj):
        try:
            style_id = obj.GraphicsStyleId
        except Exception:
            return None
        if style_id is None or style_id == ElementId.InvalidElementId:
            return None
        gs = doc.GetElement(style_id)
        if gs is None or gs.GraphicsStyleCategory is None:
            return None
        return gs.GraphicsStyleCategory.Id

    def walk(g):
        if g is None:
            return
        for obj in g:
            if isinstance(obj, GeometryInstance):
                cat_id = own_layer_id(obj)
                if cat_id in wanted_ids:
                    origin = obj.Transform.Origin
                    try:
                        nested = obj.GetInstanceGeometry()
                    except Exception:
                        nested = None
                    result['blocks'].append({
                        'position': (origin.X, origin.Y),
                        'geometry': nested,
                    })
                    continue
                try:
                    walk(obj.GetInstanceGeometry())
                except Exception:
                    pass
                continue

            if own_layer_id(obj) not in wanted_ids:
                continue

            if isinstance(obj, Arc):
                if not obj.IsBound:
                    c = obj.Center
                    result['circles'].append((c.X, c.Y, obj.Radius))
                elif include_bound_arc_chords:
                    p0, p1 = obj.GetEndPoint(0), obj.GetEndPoint(1)
                    result['lines'].append(((p0.X, p0.Y), (p1.X, p1.Y)))
                # else: bound arcs (partial arcs, fillets, symbols) skipped
            elif isinstance(obj, PolyLine):
                try:
                    result['polylines'].append(
                        [(c.X, c.Y) for c in obj.GetCoordinates()])
                except Exception:
                    pass
            elif isinstance(obj, Line):
                p0, p1 = obj.GetEndPoint(0), obj.GetEndPoint(1)
                result['lines'].append(((p0.X, p0.Y), (p1.X, p1.Y)))

    walk(geom_element)
    return result


def collect_pile_entities(doc, geom_element, target_cat_id):
    """Backwards-compatible alias: Auto Pile shipped against this name
    before the walker was generalized for Auto Column."""
    return collect_shape_entities(doc, geom_element, target_cat_id)


def pick_point_on_cad(uidoc, prompt):
    """Prompts the user to click a point on any element; returns the
    picked Reference, or None on cancel (Esc)."""
    try:
        return uidoc.Selection.PickObject(ObjectType.PointOnElement, prompt)
    except Exception:
        return None


def resolve_cad_layer(doc, ref):
    """Given a PickObject Reference, returns (import_element_id,
    layer_name). Raises ValueError('not_cad') if the picked element
    isn't part of an imported/linked CAD drawing, or ValueError with the
    underlying exception text if the layer can't be read."""
    elem = doc.GetElement(ref)
    if not isinstance(elem, ImportInstance):
        raise ValueError("not_cad")
    try:
        geo = elem.GetGeometryObjectFromReference(ref)
        gs = doc.GetElement(geo.GraphicsStyleId)
        layer_name = gs.GraphicsStyleCategory.Name
    except Exception as ex:
        raise ValueError(str(ex))
    return (elem.Id, layer_name)


def pick_points_on_cad_multi(uidoc, prompt):
    """Prompts the user to click one or more elements (Enter or right-
    click 'Finish' to confirm the whole selection, Esc to cancel) --
    lets a single "layer" pick sweep up entities that are actually
    split across several real CAD layers, which is common on multi-
    block-flattened DWG imports (confirmed against a real drawing with
    dozens of near-duplicate layer names for what is conceptually one
    layer). Returns a list of picked References, or None on cancel
    (Esc before anything was confirmed -- PickObjects raises in that
    case, same as PickObject)."""
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.PointOnElement, prompt)
        return list(refs) if refs else None
    except Exception:
        return None


def resolve_cad_layers(doc, refs):
    """Given a list of References from pick_points_on_cad_multi,
    resolves each to its CAD import + layer Category, deduplicates by
    layer, and returns (import_element_id, [Category, ...]) sorted by
    name. All picks must belong to the SAME CAD import (mirrors the
    single-pick contract: one pick session works within one import).
    Raises ValueError('not_cad') if any picked element isn't part of
    an imported/linked CAD drawing, ValueError('mixed_imports') if the
    picks span more than one CAD import, or ValueError with the
    underlying exception text if a layer can't be read."""
    import_id = None
    cats_by_id = {}
    for ref in refs:
        elem = doc.GetElement(ref)
        if not isinstance(elem, ImportInstance):
            raise ValueError("not_cad")
        if import_id is None:
            import_id = elem.Id
        elif elem.Id != import_id:
            raise ValueError("mixed_imports")
        try:
            geo = elem.GetGeometryObjectFromReference(ref)
            gs = doc.GetElement(geo.GraphicsStyleId)
            cat = gs.GraphicsStyleCategory
        except Exception as ex:
            raise ValueError(str(ex))
        cats_by_id[cat.Id] = cat
    categories = sorted(cats_by_id.values(), key=lambda c: c.Name)
    return import_id, categories


def pick_region(uidoc, prompt):
    """Prompts the user to window/crossing-select a rectangular region
    (added for Auto Beam's beam-table pick -- a single point pick isn't
    enough to bound a whole schedule table). Returns (min_xy, max_xy)
    in feet, model coordinates, or None on cancel (Esc). Only the X/Y
    of the picked box are used by callers -- in a plan view its Min/Max
    XYZ already line up with model X/Y directly, same as every other
    point pick in this extension."""
    from Autodesk.Revit.UI.Selection import PickBoxStyle
    try:
        box = uidoc.Selection.PickBox(PickBoxStyle.Crossing, prompt)
    except Exception:
        return None
    if box is None:
        return None
    return (box.Min.X, box.Min.Y), (box.Max.X, box.Max.Y)
