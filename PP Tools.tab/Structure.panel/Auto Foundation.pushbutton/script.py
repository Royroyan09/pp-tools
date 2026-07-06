# -*- coding: utf-8 -*-
"""Auto Foundation

Auto-generate 3D structural foundations from an imported/linked DWG:
pick the perimeter layer (closed polylines, one per footing) and the
label layer (F1, F2, ...), review one foundation type per unique label
with Width/Length read from the drawing, fill in Thickness, choose a
level and generate. v1 handles rectangular isolated pad footings; other
kinds plug into af_kinds / af_config.
"""
from __future__ import print_function

import os
import re
import sys

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from System.IO import FileStream, FileMode, FileAccess
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Collections.ObjectModel import ObservableCollection

from Autodesk.Revit.DB import (
    FilteredElementCollector, Element, Transaction, Level, SpecTypeId,
    UnitUtils, ImportInstance, Options, GeometryInstance, ElementId,
    Line, PolyLine
)
from Autodesk.Revit.UI.Selection import ObjectType

from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

# the bundle folder holds af_config / af_kinds; make sure it's importable
_BUNDLE_DIR = os.path.dirname(__file__)
if _BUNDLE_DIR not in sys.path:
    sys.path.insert(0, _BUNDLE_DIR)

import af_config as cfg
import af_kinds


def _safe_log(msg):
    # Writes straight to a plain text file instead of pyRevit's
    # logger/output console: on some engine threads, the console's lazy
    # initialization requires the UI thread and raising through it can
    # bring down the whole Revit process. Plain file I/O has no such risk.
    try:
        log_path = script.get_bundle_file('error.log')
        with open(log_path, 'a') as f:
            f.write(msg + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# IronPython-safe Element.Name accessor
# ---------------------------------------------------------------------------

def get_name(element):
    if element is None:
        return ""
    try:
        return Element.Name.__get__(element)
    except Exception:
        try:
            return element.Name
        except Exception:
            return "<unnamed>"


# ---------------------------------------------------------------------------
# Unit helpers (all lengths are shown/edited in the project's display units)
# ---------------------------------------------------------------------------

def get_length_unit_type_id():
    try:
        return doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()
    except Exception:
        return None


def internal_to_display_length(value_ft):
    unit_id = get_length_unit_type_id()
    try:
        return UnitUtils.ConvertFromInternalUnits(value_ft, unit_id)
    except Exception:
        return value_ft


def display_to_internal_length(text):
    unit_id = get_length_unit_type_id()
    value = float(str(text).strip().replace(",", "."))
    try:
        return UnitUtils.ConvertToInternalUnits(value, unit_id)
    except Exception:
        return value


def length_unit_label():
    unit_id = get_length_unit_type_id()
    try:
        type_id = unit_id.TypeId
    except Exception:
        return ""
    if "millimeters" in type_id:
        return "mm"
    if "centimeters" in type_id:
        return "cm"
    if "meters" in type_id:
        return "m"
    if "inches" in type_id:
        return "in"
    if "feet" in type_id:
        return "ft"
    return ""


def fmt_num(value):
    text = "{0:.4f}".format(value).rstrip('0').rstrip('.')
    return text if text else "0"


def fmt_length(value_ft):
    return fmt_num(internal_to_display_length(value_ft))


# ---------------------------------------------------------------------------
# Row view model bound to the WPF grid
# ---------------------------------------------------------------------------

class ComboItem(object):
    def __init__(self, display, value):
        self.Display = display
        self.Value = value


class TypeRow(object):
    def __init__(self, label, count, shape, width_text, length_text,
                 thickness_text, note=""):
        self.Include = True
        self.Label = label
        self.Count = count
        self.Shape = shape  # 'Rectangle' or 'Custom'
        self.Width = width_text
        self.Length = length_text
        self.Thickness = thickness_text
        self.Note = note


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class AutoFoundationWindow(forms.WPFWindow):

    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)

        self.lblDocTitle.Text = doc.Title or "Untitled"
        self._load_logo()

        # Which pick the user requested ('perimeter' / 'label'); the modal
        # dialog closes, the entry-point loop runs PickObject (which cannot
        # run while a modal window is open), then reopens with the picked
        # layer selected.
        self.pick_requested = None
        self.saved_state = None

        self._kind = af_kinds.IsolatedFooting()
        self._kind_custom = af_kinds.CustomPadFooting()
        # label -> [Footprint], filled by Apply, consumed by Generate
        self._groups = {}
        # row edits captured before a pick, reapplied after the re-scan
        self._saved_rows = {}

        unit_label = length_unit_label()
        for col, base in ((self.colWidth, "Width"),
                          (self.colLength, "Length"),
                          (self.colThickness, "Thickness")):
            col.Header = "{} ({})".format(base, unit_label) if unit_label else base

        self.dgTypes.ItemsSource = ObservableCollection[object]()

        self._load_cad_imports()
        self._load_levels()
        self._update_family_info()

        self.cmbCadImport.SelectionChanged += self.on_cad_import_changed
        self.btnPickPerim.Click += lambda s, e: self._request_pick('perimeter')
        self.btnPickLabel.Click += lambda s, e: self._request_pick('label')
        self.btnApply.Click += self.on_apply
        self.btnGenerate.Click += self.on_generate
        self.btnClose.Click += self.on_close

        # the initial SelectedIndex is set before events are wired, so the
        # layer combos would otherwise stay empty until the user reselects
        # the import by hand
        self.on_cad_import_changed(None, None)

    # ------------------------------------------------------------------

    def _load_logo(self):
        # Loaded via FileStream rather than a System.Uri: on this pyRevit
        # engine, `from System import Uri` fails to resolve at import time
        # (raised outside any try/except, since it's a module-level import),
        # which pyRevit's own error reporting then chokes on.
        try:
            logo_path = script.get_bundle_file('logo.png')
            if not logo_path:
                return
            stream = FileStream(logo_path, FileMode.Open, FileAccess.Read)
            try:
                bmp = BitmapImage()
                bmp.BeginInit()
                bmp.StreamSource = stream
                bmp.CacheOption = BitmapCacheOption.OnLoad
                bmp.EndInit()
            finally:
                stream.Close()
            self.imgLogo.Source = bmp
        except Exception as ex:
            _safe_log("_load_logo failed: {}".format(ex))

    # ------------------------------------------------------------------
    # Combo population
    # ------------------------------------------------------------------

    def _load_cad_imports(self):
        imports = FilteredElementCollector(doc).OfClass(ImportInstance).ToElements()
        items = []
        for imp in imports:
            cat = imp.Category
            name = cat.Name if cat is not None else get_name(imp)
            if not name:
                name = "CAD Import"
            items.append(ComboItem(name, imp))
        self.cmbCadImport.ItemsSource = items
        if items:
            self.cmbCadImport.SelectedIndex = 0
        else:
            self.lblInfo.Text = "No imported or linked CAD drawings were found in this model."

    def on_cad_import_changed(self, sender, e):
        item = self.cmbCadImport.SelectedItem
        self.cmbPerimLayer.ItemsSource = None
        self.cmbLabelLayer.ItemsSource = None
        if item is None:
            return
        imp = item.Value
        cat = imp.Category
        layers = []
        if cat is not None:
            try:
                for sub in cat.SubCategories:
                    layers.append(sub)
            except Exception:
                pass
        layers.sort(key=lambda c: c.Name)
        self.cmbPerimLayer.ItemsSource = [ComboItem(c.Name, c) for c in layers]
        self.cmbLabelLayer.ItemsSource = [ComboItem(c.Name, c) for c in layers]
        # pre-select by name hints only; picking in the drawing always wins
        self._preselect_by_hints(self.cmbPerimLayer, cfg.PERIMETER_LAYER_HINTS)
        self._preselect_by_hints(self.cmbLabelLayer, cfg.LABEL_LAYER_HINTS)

    def _preselect_by_hints(self, combo, hints):
        for item in (combo.ItemsSource or []):
            upper = item.Display.upper()
            for hint in hints:
                if hint.upper() in upper:
                    combo.SelectedItem = item
                    return

    def _load_levels(self):
        levels = list(FilteredElementCollector(doc).OfClass(Level).ToElements())
        levels.sort(key=lambda lv: lv.Elevation)
        unit_label = length_unit_label()
        items = []
        for lv in levels:
            disp = u"{}  ({} {})".format(get_name(lv), fmt_length(lv.Elevation), unit_label)
            items.append(ComboItem(disp, lv))
        self.cmbLevel.ItemsSource = items
        if items:
            # foundations usually sit on the lowest level
            self.cmbLevel.SelectedIndex = 0

    def _update_family_info(self):
        base_note = ("Footings are placed with their TOP flush with the "
                     "selected level; thickness extends downward. Rectangles "
                     "use the footing family (auto-rotated to the CAD); "
                     "custom/triangular shapes become foundation slabs from "
                     "the exact outline. ")
        try:
            sym = self._kind.find_base_symbol(doc)
        except Exception:
            sym = None
        if sym is None:
            self.lblFamilyInfo.Text = base_note + (
                "Family '{}' is not loaded yet — Generate will load it from "
                "the Revit library automatically.".format(cfg.FAMILY_NAME_CANDIDATES[0]))
            return
        params = self._kind.resolve_params(sym)
        missing = [k for k in ('width', 'length', 'thickness') if not params.get(k)]
        if missing:
            self.lblFamilyInfo.Text = base_note + (
                "WARNING: family '{}' is loaded but its {} parameter(s) could "
                "not be identified — check af_config.py.".format(
                    sym.Family.Name, ", ".join(missing)))
        else:
            self.lblFamilyInfo.Text = base_note + (
                "Family '{}' is loaded (parameters: {}, {}, {}).".format(
                    sym.Family.Name, params['width'], params['length'],
                    params['thickness']))

    # ------------------------------------------------------------------
    # Pick handling (state round-trips through the entry-point loop)
    # ------------------------------------------------------------------

    def _request_pick(self, which):
        if not (self.cmbCadImport.ItemsSource or []):
            forms.alert("No imported or linked CAD drawings were found in this model.")
            return
        self.saved_state = self.capture_state()
        self.pick_requested = which
        self.Close()

    def capture_state(self):
        state = {
            'import_id': None, 'perim_layer': None, 'label_layer': None,
            'level_id': None, 'rows': {},
        }
        if self.cmbCadImport.SelectedItem is not None:
            state['import_id'] = self.cmbCadImport.SelectedItem.Value.Id
        if self.cmbPerimLayer.SelectedItem is not None:
            state['perim_layer'] = self.cmbPerimLayer.SelectedItem.Display
        if self.cmbLabelLayer.SelectedItem is not None:
            state['label_layer'] = self.cmbLabelLayer.SelectedItem.Display
        if self.cmbLevel.SelectedItem is not None:
            state['level_id'] = self.cmbLevel.SelectedItem.Value.Id
        for row in (self.dgTypes.ItemsSource or []):
            state['rows'][row.Label] = (row.Width, row.Length, row.Thickness)
        return state

    def initialize_session(self, state, pending_pick):
        """Called by the entry point on (re)open: restores combo/row state
        captured before a pick, applies the pick result, and re-runs the
        scan when both layers are known."""
        try:
            if state:
                self._select_import(state.get('import_id'))
                self._select_layer(self.cmbPerimLayer, state.get('perim_layer'))
                self._select_layer(self.cmbLabelLayer, state.get('label_layer'))
                self._select_level(state.get('level_id'))
                self._saved_rows = state.get('rows') or {}
            if pending_pick:
                which, import_id, layer_name = pending_pick
                self._select_import(import_id)
                combo = self.cmbPerimLayer if which == 'perimeter' else self.cmbLabelLayer
                self._select_layer(combo, layer_name)
            if (self.cmbPerimLayer.SelectedItem is not None
                    and self.cmbLabelLayer.SelectedItem is not None
                    and (state or pending_pick)):
                self.on_apply(None, None)
        except Exception as ex:
            _safe_log("initialize_session failed: {}".format(ex))

    def _select_import(self, import_id):
        if import_id is None:
            return
        for item in (self.cmbCadImport.ItemsSource or []):
            if item.Value.Id == import_id:
                self.cmbCadImport.SelectedItem = item
                return

    def _select_layer(self, combo, layer_name):
        if not layer_name:
            return
        for item in (combo.ItemsSource or []):
            if item.Display == layer_name:
                combo.SelectedItem = item
                return

    def _select_level(self, level_id):
        if level_id is None:
            return
        for item in (self.cmbLevel.ItemsSource or []):
            if item.Value.Id == level_id:
                self.cmbLevel.SelectedItem = item
                return

    # ------------------------------------------------------------------
    # Scan (Apply)
    # ------------------------------------------------------------------

    def _try_get_text_class(self):
        # Newer Revit API versions expose CAD text as Autodesk.Revit.DB.Text
        # geometry objects with a readable .Value string. Older versions
        # don't have this class at all, so the import itself must be
        # attempted lazily (inside a function, wrapped in try/except) rather
        # than at module load time.
        try:
            from Autodesk.Revit.DB import Text as DBText
            return DBText
        except Exception:
            return None

    def _walk_geometry(self, geom_element, perim_cat_id, label_cat_id,
                       polylines, lines, texts, DBText):
        if geom_element is None:
            return
        for obj in geom_element:
            if isinstance(obj, GeometryInstance):
                try:
                    # instance geometry is already transformed into Revit
                    # model coordinates, so no manual transform is needed
                    inst_geom = obj.GetInstanceGeometry()
                except Exception:
                    inst_geom = None
                self._walk_geometry(inst_geom, perim_cat_id, label_cat_id,
                                    polylines, lines, texts, DBText)
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

            if cat_id == perim_cat_id:
                if isinstance(obj, PolyLine):
                    try:
                        polylines.append([(c.X, c.Y) for c in obj.GetCoordinates()])
                    except Exception:
                        pass
                elif isinstance(obj, Line):
                    p0, p1 = obj.GetEndPoint(0), obj.GetEndPoint(1)
                    lines.append(((p0.X, p0.Y), (p1.X, p1.Y)))
            elif label_cat_id is not None and cat_id == label_cat_id:
                if DBText is not None and isinstance(obj, DBText):
                    try:
                        texts.append((obj.Position, obj.Value))
                    except Exception:
                        pass

    def on_apply(self, sender, e):
        cad_item = self.cmbCadImport.SelectedItem
        perim_item = self.cmbPerimLayer.SelectedItem
        label_item = self.cmbLabelLayer.SelectedItem
        if cad_item is None or perim_item is None:
            forms.alert("Choose a CAD import and a perimeter layer first "
                        "(or use Pick Layer).", title="Auto Foundation")
            return
        if label_item is None:
            forms.alert("Choose a label layer first (or use Pick Label).",
                        title="Auto Foundation")
            return

        try:
            opts = Options()
            opts.IncludeNonVisibleObjects = True
            geom = cad_item.Value.get_Geometry(opts)
        except Exception as ex:
            forms.alert("Could not read geometry from this CAD import:\n{}".format(ex))
            return

        DBText = self._try_get_text_class()
        polylines, lines, texts = [], [], []
        self._walk_geometry(geom, perim_item.Value.Id, label_item.Value.Id,
                            polylines, lines, texts, DBText)

        tol_ft = cfg.CHAIN_TOLERANCE_MM * cfg.MM_TO_FT
        outlines = []
        for pts in polylines:
            # drop the repeated closing vertex; closure is implicit
            if len(pts) >= 2 and (abs(pts[0][0] - pts[-1][0]) <= tol_ft
                                  and abs(pts[0][1] - pts[-1][1]) <= tol_ft):
                pts = pts[:-1]
            if len(pts) >= 3:
                outlines.append(pts)
        # exploded rectangles: chain loose line segments into closed loops
        outlines.extend(af_kinds.chain_lines_to_loops(lines, tol_ft))

        min_side = cfg.MIN_FOOTPRINT_SIDE_MM * cfg.MM_TO_FT
        max_side = cfg.MAX_FOOTPRINT_SIDE_MM * cfg.MM_TO_FT
        footprints = []
        skipped = 0
        for pts in outlines:
            fp = af_kinds.Footprint(pts)
            if fp.width_ft < min_side or fp.length_ft > max_side:
                skipped += 1
                continue
            footprints.append(fp)

        if not footprints:
            self.dgTypes.ItemsSource = ObservableCollection[object]()
            self._groups = {}
            self.lblInfo.Text = (
                "No closed outlines were found on layer '{}' (checked {} "
                "polylines and {} loose lines; {} filtered out by size). Check "
                "the layer or the size limits in af_config.py.".format(
                    perim_item.Display, len(polylines), len(lines), skipped))
            self.lblStatus.Text = "Scan found 0 footings."
            return

        clean_texts = []
        for pos, val in texts:
            v = (val or u"").strip()
            if cfg.LABEL_UPPERCASE:
                v = v.upper()
            if not v:
                continue
            if cfg.LABEL_REGEX and not re.match(cfg.LABEL_REGEX, v):
                continue
            clean_texts.append((pos.X, pos.Y, v))

        matched = self._kind.match_labels(footprints, clean_texts)
        self._groups = self._kind.group_footprints(footprints)

        mismatch_tol = cfg.SIZE_MISMATCH_TOLERANCE_MM * cfg.MM_TO_FT
        coll = ObservableCollection[object]()
        for label, fps in self._groups.items():
            w = max(fp.width_ft for fp in fps)
            l = max(fp.length_ft for fp in fps)
            # a group is placed as a rectangular family only when every
            # footing in it is rectangular; otherwise the exact outlines
            # become foundation slabs
            is_rect = all(fp.is_rectangle for fp in fps)
            rotated = sum(1 for fp in fps
                          if fp.is_rectangle and abs(fp.rotation) > 1e-6
                          and abs(abs(fp.rotation) - 1.5707963) > 1e-6)
            notes = []
            if not is_rect:
                notes.append("exact CAD outline used; W/L for reference")
            elif rotated:
                notes.append("{} rotated to match CAD".format(rotated))
            if any(abs(fp.width_ft - w) > mismatch_tol
                   or abs(fp.length_ft - l) > mismatch_tol for fp in fps):
                notes.append("sizes varied in CAD; largest used")
            th_text = ""
            if label in cfg.DEFAULT_THICKNESS_MM:
                th_text = fmt_length(cfg.DEFAULT_THICKNESS_MM[label] * cfg.MM_TO_FT)
            row = TypeRow(label, len(fps), "Rectangle" if is_rect else "Custom",
                          fmt_length(w), fmt_length(l), th_text, "; ".join(notes))
            saved = self._saved_rows.get(label)
            if saved:
                row.Width, row.Length, row.Thickness = saved
            coll.Add(row)
        self.dgTypes.ItemsSource = coll
        self._saved_rows = {}

        text_note = ""
        if DBText is None and texts == []:
            text_note = (" NOTE: CAD text could not be read on this Revit "
                         "version; footings were grouped by size instead.")
        self.lblInfo.Text = (
            "Found {} footing(s) in {} type group(s) on layer '{}'; {} labelled "
            "from CAD text on layer '{}' ({} text entities read).{}{} Review the "
            "sizes, fill in Thickness and click Generate.".format(
                len(footprints), coll.Count, perim_item.Display, matched,
                label_item.Display, len(clean_texts),
                " {} outline(s) were filtered out by size.".format(skipped) if skipped else "",
                text_note))
        self.lblStatus.Text = "Scan complete: {} footing(s), {} type(s).".format(
            len(footprints), coll.Count)

    # ------------------------------------------------------------------
    # Generate
    # ------------------------------------------------------------------

    def on_generate(self, sender, e):
        coll = self.dgTypes.ItemsSource
        if coll is None or coll.Count == 0:
            forms.alert("Nothing to generate — pick the layers and click "
                        "Apply first.", title="Auto Foundation")
            return
        level_item = self.cmbLevel.SelectedItem
        if level_item is None:
            forms.alert("Choose a base level.", title="Auto Foundation")
            return

        rect_groups = []
        custom_groups = []
        errors = []
        for row in coll:
            if not row.Include:
                continue
            is_rect = row.Shape == "Rectangle"
            w = l = 0.0
            if is_rect:
                try:
                    w = display_to_internal_length(row.Width)
                    l = display_to_internal_length(row.Length)
                except Exception:
                    errors.append(u"'{}': invalid Width/Length.".format(row.Label))
                    continue
            th_text = (row.Thickness or u"").strip()
            if not th_text:
                errors.append(u"'{}': Thickness is required.".format(row.Label))
                continue
            try:
                th = display_to_internal_length(th_text)
            except Exception:
                errors.append(u"'{}': invalid Thickness '{}'.".format(row.Label, row.Thickness))
                continue
            if th <= 0 or (is_rect and (w <= 0 or l <= 0)):
                errors.append(u"'{}': Width, Length and Thickness must be "
                              u"greater than zero.".format(row.Label))
                continue
            group = {
                'label': row.Label,
                # keep the convention: longer side = Length
                'width_ft': min(w, l),
                'length_ft': max(w, l),
                'thickness_ft': th,
                'footprints': self._groups.get(row.Label, []),
            }
            (rect_groups if is_rect else custom_groups).append(group)

        if errors:
            forms.alert(u"Fix these rows first:\n\n{}".format(u"\n".join(errors)),
                        title="Auto Foundation")
            return
        if not rect_groups and not custom_groups:
            forms.alert("No rows are checked for generation.", title="Auto Foundation")
            return

        level = level_item.Value
        groups_total = len(rect_groups) + len(custom_groups)
        t = Transaction(doc, "Auto Foundation: Generate")
        t.Start()
        try:
            created = 0
            warnings = []
            if rect_groups:
                n, warn = self._kind.generate(doc, level, rect_groups)
                created += n
                warnings.extend(warn)
            if custom_groups:
                n, warn = self._kind_custom.generate(doc, level, custom_groups)
                created += n
                warnings.extend(warn)
            t.Commit()
        except ValueError as ex:
            t.RollBack()
            forms.alert(str(ex), title="Auto Foundation")
            return
        except Exception as ex:
            t.RollBack()
            _safe_log("Generate failed: {}".format(ex))
            forms.alert("Failed to generate foundations:\n{}".format(ex),
                        title="Auto Foundation")
            self.lblStatus.Text = "Failed."
            return

        self._update_family_info()
        if warnings:
            forms.alert(
                u"Placed {} footing(s) in {} type(s).\n\nSome items had "
                u"problems:\n{}".format(created, groups_total, u"\n".join(warnings)),
                title="Auto Foundation")
            self.lblStatus.Text = "Placed {} footing(s), {} warning(s).".format(
                created, len(warnings))
        else:
            self.lblStatus.Text = "Placed {} footing(s) in {} type(s) on {}.".format(
                created, groups_total, get_name(level))

    def on_close(self, sender, e):
        self.Close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if doc is None:
    forms.alert("No active Revit document.", exitscript=True)


def _pick_cad_layer(prompt):
    """Prompts the user to click an entity on a CAD import in the model;
    returns (import_element_id, layer_name), or None on cancel."""
    try:
        ref = uidoc.Selection.PickObject(ObjectType.PointOnElement, prompt)
    except Exception:
        return None

    elem = doc.GetElement(ref)
    if not isinstance(elem, ImportInstance):
        forms.alert("The picked element is not part of an imported/linked "
                    "CAD drawing. Pick an entity on the CAD.",
                    title="Auto Foundation")
        return None
    try:
        geo = elem.GetGeometryObjectFromReference(ref)
        gs = doc.GetElement(geo.GraphicsStyleId)
        layer_name = gs.GraphicsStyleCategory.Name
    except Exception as ex:
        _safe_log("_pick_cad_layer failed: {}".format(ex))
        forms.alert("Could not read the layer of the picked object.",
                    title="Auto Foundation")
        return None
    return (elem.Id, layer_name)


PICK_PROMPTS = {
    'perimeter': "Pick a footing perimeter curve on the CAD drawing (Esc to cancel)",
    'label': "Pick a footing label text (F1, F2, ...) on the CAD drawing (Esc to cancel)",
}

try:
    xaml_file = script.get_bundle_file('ui.xaml')
    # The dialog is modal, so element picking has to happen between dialog
    # sessions: Pick Layer / Pick Label close the window with pick_requested
    # set, we pick here, then reopen with the picked layer selected.
    state = None
    pending_pick = None
    while True:
        window = AutoFoundationWindow(xaml_file)
        window.initialize_session(state, pending_pick)
        pending_pick = None
        window.ShowDialog()
        if not window.pick_requested:
            break
        state = window.saved_state
        result = _pick_cad_layer(PICK_PROMPTS[window.pick_requested])
        if result is not None:
            pending_pick = (window.pick_requested, result[0], result[1])
except Exception as ex:
    import traceback
    _safe_log("Entry point failed: {}\n{}".format(ex, traceback.format_exc()))
