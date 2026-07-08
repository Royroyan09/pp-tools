# -*- coding: utf-8 -*-
"""Auto Pile

Auto-generate 3D structural piles from an imported/linked DWG: pick the
perimeter layer (circles, closed polylines, or block references, one
per pile) and the label layer (or check "No labels" to group purely by
size), review one pile type per unique label with Diameter (round) or
Width/Length (square) read from the drawing, fill in Depth, choose a
level and generate. Round piles use M_Pile_Beton (or equivalent) from
Revit's library; square piles use the bundled PP_Pile-Square-Concrete
family, auto-rotated to match the CAD outline. Custom (non-circular,
non-rectangular) outlines are classified but not yet generated — see
ap_kinds.CustomPileFamily.

CAD reading, label matching and placement are shared with Auto
Foundation via the extension's pp_common lib.
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

from Autodesk.Revit.DB import FilteredElementCollector, Level, Options, Transaction

from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

_BUNDLE_DIR = os.path.dirname(__file__)
if _BUNDLE_DIR not in sys.path:
    sys.path.insert(0, _BUNDLE_DIR)


def _find_extension_lib_dir(start_dir):
    """Walks upward from start_dir to the enclosing '*.extension'
    folder and returns its 'lib' subfolder. See Auto Foundation's
    script.py for the twin copy of this bootstrap."""
    d = os.path.abspath(start_dir)
    for _ in range(8):
        if d.lower().endswith(".extension"):
            return os.path.join(d, "lib")
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


_LIB_DIR = _find_extension_lib_dir(_BUNDLE_DIR)
if _LIB_DIR and _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import ap_config as cfg
import ap_shapes
import ap_kinds
from pp_common import cad_read
from pp_common import units as pp_units
from pp_common import logging_util as pp_logging
from pp_common import dxf_text as pp_dxf
from pp_common.config_base import MM_TO_FT
from pp_common.geometry import round_dimension_mm
from pp_common.wpf_helpers import (
    ComboItem, LayerSelection, set_layer_selection, select_layers_by_name)


def _safe_log(msg):
    try:
        log_path = script.get_bundle_file('error.log')
        pp_logging.safe_log(log_path, msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Unit helpers (delegated to pp_common; module-level wrappers so call
# sites below don't need to pass doc around)
# ---------------------------------------------------------------------------

def get_name(element):
    return pp_units.get_name(element)


def length_unit_label():
    return pp_units.length_unit_label(doc)


def internal_to_display_length(value_ft):
    return pp_units.internal_to_display_length(doc, value_ft)


def display_to_internal_length(text):
    return pp_units.display_to_internal_length(doc, text)


def fmt_length(value_ft):
    return pp_units.fmt_length(doc, value_ft)


# ---------------------------------------------------------------------------
# Row view model bound to the WPF grid
# ---------------------------------------------------------------------------

class TypeRow(object):
    def __init__(self, label, count, shape, size1_text, size2_text,
                 depth_text, named_via, note=""):
        self.Include = True
        self.Label = label
        self.Count = count
        self.Shape = shape          # 'Circle' | 'Square' | 'Custom'
        self.Size1 = size1_text     # Diameter (circle) or Width (square/custom)
        self.Size2 = size2_text     # blank for circle/true-square
        self.Depth = depth_text
        self.NamedVia = named_via   # 'CAD text' | 'size-derived'
        self.Note = note


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class AutoPileWindow(forms.WPFWindow):

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

        self._round_kind = ap_kinds.RoundPileFamily()
        self._square_kind = ap_kinds.SquarePileFamily()
        # label -> [shape], filled by Apply, consumed by Generate
        self._groups = {}
        # row edits captured before a pick, reapplied after the re-scan
        self._saved_rows = {}

        unit_label = length_unit_label()
        for col, base in ((self.colSize1, "Diameter / Width"),
                          (self.colSize2, "Length (if not square)"),
                          (self.colDepth, "Depth")):
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
        self.chkNoLabels.Checked += self.on_no_labels_toggled
        self.chkNoLabels.Unchecked += self.on_no_labels_toggled
        self.btnBatchFillDepth.Click += self.on_batch_fill_depth

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
        imports = cad_read.list_cad_imports(doc)
        items = [ComboItem(name, imp) for name, imp in imports]
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
        layers = cad_read.list_layers(item.Value)
        self.cmbPerimLayer.ItemsSource = [ComboItem(c.Name, LayerSelection([c])) for c in layers]
        self.cmbLabelLayer.ItemsSource = [ComboItem(c.Name, LayerSelection([c])) for c in layers]
        self._preselect_by_hints(self.cmbPerimLayer, cfg.PERIMETER_LAYER_HINTS)
        self._preselect_by_hints(self.cmbLabelLayer, cfg.LABEL_LAYER_HINTS)

    def _preselect_by_hints(self, combo, hints):
        for item in (combo.ItemsSource or []):
            upper = item.Display.upper()
            for hint in hints:
                if hint.upper() in upper:
                    combo.SelectedItem = item
                    return

    def on_no_labels_toggled(self, sender, e):
        no_labels = self.chkNoLabels.IsChecked
        self.cmbLabelLayer.IsEnabled = not no_labels
        self.btnPickLabel.IsEnabled = not no_labels
        self.lblLabelLayer.IsEnabled = not no_labels

    def on_batch_fill_depth(self, sender, e):
        text = (self.txtBatchDepth.Text or u"").strip()
        if not text:
            forms.alert("Type a Depth value first.", title="Auto Pile")
            return
        selected = list(self.dgTypes.SelectedItems or [])
        if not selected:
            forms.alert("Select one or more rows in the grid first "
                        "(Ctrl/Shift-click), then Fill Selected Rows.",
                        title="Auto Pile")
            return
        for row in selected:
            row.Depth = text
        self.dgTypes.Items.Refresh()
        self.lblStatus.Text = "Filled Depth for {} row(s).".format(len(selected))

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
            self.cmbLevel.SelectedIndex = 0

    def _update_family_info(self):
        base_note = ("Piles are placed with their TOP flush with the "
                     "selected level; depth extends downward. ")
        parts = []
        for kind in (self._round_kind, self._square_kind):
            try:
                sym = kind.find_base_symbol(doc)
            except Exception:
                sym = None
            if sym is None:
                parts.append(
                    "{} family not loaded yet (will be loaded automatically "
                    "on Generate).".format(kind.display_name))
                continue
            params = kind.resolve_params(sym)
            missing = [k for k in kind.size_param_candidates if not params.get(k)]
            if not params.get('depth'):
                missing.append('depth')
            if missing:
                parts.append(
                    "WARNING: {} family '{}' is loaded but its {} "
                    "parameter(s) could not be identified.".format(
                        kind.display_name, sym.Family.Name, ", ".join(missing)))
            else:
                mat_note = (" (material: {})".format(params['material'])
                           if params.get('material') else " (no material parameter)")
                parts.append("{} family '{}' OK{}.".format(
                    kind.display_name.capitalize(), sym.Family.Name, mat_note))
        self.lblFamilyInfo.Text = base_note + " ".join(parts)

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
            'import_id': None, 'perim_layer_names': None, 'label_layer_names': None,
            'level_id': None, 'rows': {}, 'no_labels': self.chkNoLabels.IsChecked,
        }
        if self.cmbCadImport.SelectedItem is not None:
            state['import_id'] = self.cmbCadImport.SelectedItem.Value.Id
        if self.cmbPerimLayer.SelectedItem is not None:
            state['perim_layer_names'] = self.cmbPerimLayer.SelectedItem.Value.names
        if self.cmbLabelLayer.SelectedItem is not None:
            state['label_layer_names'] = self.cmbLabelLayer.SelectedItem.Value.names
        if self.cmbLevel.SelectedItem is not None:
            state['level_id'] = self.cmbLevel.SelectedItem.Value.Id
        for row in (self.dgTypes.ItemsSource or []):
            state['rows'][row.Label] = (row.Size1, row.Size2, row.Depth)
        return state

    def initialize_session(self, state, pending_pick):
        """Called by the entry point on (re)open: restores combo/row state
        captured before a pick, applies the pick result, and re-runs the
        scan when the perimeter layer (and, unless no-label mode is on,
        the label layer) is known."""
        try:
            if state:
                self._select_import(state.get('import_id'))
                select_layers_by_name(self.cmbPerimLayer, state.get('perim_layer_names'))
                select_layers_by_name(self.cmbLabelLayer, state.get('label_layer_names'))
                self._select_level(state.get('level_id'))
                self._saved_rows = state.get('rows') or {}
                self.chkNoLabels.IsChecked = bool(state.get('no_labels'))
                self.on_no_labels_toggled(None, None)
            if pending_pick:
                which, import_id, categories = pending_pick
                self._select_import(import_id)
                combo = self.cmbPerimLayer if which == 'perimeter' else self.cmbLabelLayer
                set_layer_selection(combo, categories)
            label_ready = self.chkNoLabels.IsChecked or self.cmbLabelLayer.SelectedItem is not None
            if (self.cmbPerimLayer.SelectedItem is not None and label_ready
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

    def on_apply(self, sender, e):
        cad_item = self.cmbCadImport.SelectedItem
        perim_item = self.cmbPerimLayer.SelectedItem
        no_labels = self.chkNoLabels.IsChecked
        label_item = self.cmbLabelLayer.SelectedItem
        if cad_item is None or perim_item is None:
            forms.alert("Choose a CAD import and a perimeter layer first "
                        "(or use Pick Layer).", title="Auto Pile")
            return
        if not no_labels and label_item is None:
            forms.alert("Choose a label layer first (or use Pick Label), or "
                        "check 'No labels in this CAD'.", title="Auto Pile")
            return

        shapes, report = ap_shapes.scan_perimeter_layer(
            doc, cad_item.Value, perim_item.Value, cfg)

        if not shapes:
            self.dgTypes.ItemsSource = ObservableCollection[object]()
            self._groups = {}
            self.lblInfo.Text = (
                "No circles, closed polylines, or block references were "
                "found on layer '{}'. Pick a different layer, or check the "
                "size limits in ap_config.py.".format(perim_item.Display))
            self.lblStatus.Text = "Scan found 0 piles."
            return

        if no_labels:
            # skip CAD text entirely -- every pile is named purely from
            # its own derived size (D800, S800, WxL)
            matched = 0
            clean_texts = []
            text_source = "none (no-label mode)"
            groups = ap_shapes.group_by_size_only(shapes)
        else:
            # --- read labels: CAD geometry first, DXF-export fallback ---
            opts = Options()
            opts.IncludeNonVisibleObjects = True
            geom = cad_item.Value.get_Geometry(opts)
            DBText = cad_read.try_get_text_class()
            buckets = cad_read.collect_layer_geometry(
                doc, geom, {'label': label_item.Value}, DBText)
            raw_texts = [(pos.X, pos.Y, val) for pos, val in buckets['label']['texts']]
            text_source = "CAD geometry"
            if not raw_texts:
                try:
                    raw_texts = pp_dxf.read_cad_texts(doc, label_item.Display)
                    text_source = "DXF export"
                except Exception as ex:
                    _safe_log("DXF text fallback failed: {}".format(ex))

            clean_texts = []
            for tx, ty, val in raw_texts:
                v = (val or u"").strip()
                if cfg.LABEL_UPPERCASE:
                    v = v.upper()
                if v and (not cfg.LABEL_REGEX or re.match(cfg.LABEL_REGEX, v)):
                    clean_texts.append((tx, ty, v))

            matched, groups = ap_shapes.match_and_group(shapes, clean_texts, cfg)

        self._groups = groups

        mismatch_tol = cfg.SIZE_MISMATCH_TOLERANCE_MM * cfg.MM_TO_FT
        coll = ObservableCollection[object]()
        for label, shs in groups.items():
            row = self._build_row(label, shs, clean_texts, mismatch_tol)
            saved = self._saved_rows.get(label)
            if saved:
                row.Size1, row.Size2, row.Depth = saved
            coll.Add(row)
        self.dgTypes.ItemsSource = coll
        self._saved_rows = {}

        skipped_note = ""
        if report['blocks_unresolved']:
            skipped_note = " {} block reference(s) could not be classified.".format(
                report['blocks_unresolved'])

        if no_labels:
            label_note = "no-label mode — grouped purely by Diameter/Width x Length."
        else:
            label_note = "{} labelled from CAD text ({} text entities via {}).".format(
                matched, len(clean_texts), text_source)

        self.lblInfo.Text = (
            "Found {} pile(s) in {} type group(s) on layer '{}' "
            "(circles: {circles}, square polylines: {square_polylines}, "
            "rect polylines: {rect_polylines}, custom: {custom_polylines}, "
            "blocks: {block_total}); {}{} Review the sizes, fill in Depth "
            "and click Generate.".format(
                len(shapes), coll.Count, perim_item.Display, label_note,
                skipped_note,
                block_total=(report['blocks_circle'] + report['blocks_square']
                            + report['blocks_rect'] + report['blocks_custom']),
                **report))
        self.lblStatus.Text = "Scan complete: {} pile(s), {} type(s).".format(
            len(shapes), coll.Count)

    def _build_row(self, label, shapes, clean_texts, mismatch_tol):
        s0 = shapes[0]
        via_cad_text = any(t[2] == label for t in clean_texts)
        named_via = "CAD text" if via_cad_text else "size-derived"

        rounded = False
        if isinstance(s0, ap_shapes.CircleShape):
            shape_kind = "Circle"
            d = max(s.diameter_ft for s in shapes)
            d_mm, rounded = round_dimension_mm(d / MM_TO_FT)
            d = d_mm * MM_TO_FT
            size1, size2 = fmt_length(d), ""
            mismatch = any(abs(s.diameter_ft - d) > mismatch_tol for s in shapes)
        elif isinstance(s0, ap_shapes.SquareShape):
            shape_kind = "Square"
            w = max(s.width_ft for s in shapes)
            l = max(s.length_ft for s in shapes)
            is_square = all(s.is_square for s in shapes)
            w_mm, w_rounded = round_dimension_mm(w / MM_TO_FT)
            l_mm, l_rounded = round_dimension_mm(l / MM_TO_FT)
            w, l = w_mm * MM_TO_FT, l_mm * MM_TO_FT
            rounded = w_rounded or l_rounded
            size1 = fmt_length(w)
            size2 = "" if is_square else fmt_length(l)
            mismatch = any(abs(s.width_ft - w) > mismatch_tol
                          or abs(s.length_ft - l) > mismatch_tol for s in shapes)
        else:
            shape_kind = "Custom"
            w = max(s.width_ft for s in shapes)
            l = max(s.length_ft for s in shapes)
            size1, size2 = fmt_length(w), fmt_length(l)
            mismatch = any(abs(s.width_ft - w) > mismatch_tol
                          or abs(s.length_ft - l) > mismatch_tol for s in shapes)

        notes = []
        if shape_kind == "Custom":
            notes.append("no custom pile family yet; generation will skip this type")
        if rounded:
            notes.append("rounded to a clean size")
        if mismatch:
            notes.append("sizes varied in CAD; largest used")
        if len(set(s.display_name for s in shapes)) > 1:
            notes.append("mixed shapes under one label; review")

        return TypeRow(label, len(shapes), shape_kind, size1, size2, "",
                      named_via, "; ".join(notes))

    # ------------------------------------------------------------------
    # Generate
    # ------------------------------------------------------------------

    def on_generate(self, sender, e):
        coll = self.dgTypes.ItemsSource
        if coll is None or coll.Count == 0:
            forms.alert("Nothing to generate — pick the layers and click "
                        "Apply first.", title="Auto Pile")
            return
        level_item = self.cmbLevel.SelectedItem
        if level_item is None:
            forms.alert("Choose a base level.", title="Auto Pile")
            return

        round_groups = []
        square_groups = []
        skipped_custom = []
        errors = []
        for row in coll:
            if not row.Include:
                continue
            if row.Shape == "Custom":
                skipped_custom.append(row.Label)
                continue

            depth_text = (row.Depth or u"").strip()
            if not depth_text:
                errors.append(u"'{}': Depth is required.".format(row.Label))
                continue
            try:
                depth_ft = display_to_internal_length(depth_text)
            except Exception:
                errors.append(u"'{}': invalid Depth '{}'.".format(row.Label, row.Depth))
                continue
            if depth_ft <= 0:
                errors.append(u"'{}': Depth must be greater than zero.".format(row.Label))
                continue

            shapes = self._groups.get(row.Label, [])
            if row.Shape == "Circle":
                try:
                    d_ft = display_to_internal_length(row.Size1)
                except Exception:
                    errors.append(u"'{}': invalid Diameter '{}'.".format(row.Label, row.Size1))
                    continue
                if d_ft <= 0:
                    errors.append(u"'{}': Diameter must be greater than zero.".format(row.Label))
                    continue
                round_groups.append({
                    'label': row.Label, 'diameter_ft': d_ft,
                    'depth_ft': depth_ft, 'shapes': shapes,
                })
            else:  # "Square" (includes true squares and W x L rectangles)
                try:
                    w_ft = display_to_internal_length(row.Size1)
                except Exception:
                    errors.append(u"'{}': invalid Width '{}'.".format(row.Label, row.Size1))
                    continue
                size2_text = (row.Size2 or u"").strip()
                if size2_text:
                    try:
                        l_ft = display_to_internal_length(size2_text)
                    except Exception:
                        errors.append(u"'{}': invalid Length '{}'.".format(row.Label, row.Size2))
                        continue
                else:
                    l_ft = w_ft
                if w_ft <= 0 or l_ft <= 0:
                    errors.append(u"'{}': Width and Length must be greater "
                                  u"than zero.".format(row.Label))
                    continue
                square_groups.append({
                    'label': row.Label, 'width_ft': min(w_ft, l_ft),
                    'length_ft': max(w_ft, l_ft), 'depth_ft': depth_ft,
                    'shapes': shapes,
                })

        if errors:
            forms.alert(u"Fix these rows first:\n\n{}".format(u"\n".join(errors)),
                        title="Auto Pile")
            return
        if not round_groups and not square_groups:
            msg = "No rows are checked for generation."
            if skipped_custom:
                msg += (u"\n\n{} custom-shape type(s) were skipped (no "
                       u"custom pile family yet): {}".format(
                           len(skipped_custom), u", ".join(skipped_custom)))
            forms.alert(msg, title="Auto Pile")
            return

        level = level_item.Value
        groups_total = len(round_groups) + len(square_groups)
        t = Transaction(doc, "Auto Pile: Generate")
        t.Start()
        try:
            created = 0
            warnings = []
            if round_groups:
                n, warn = self._round_kind.generate(doc, level, round_groups,
                                                    bundle_dir=_BUNDLE_DIR)
                created += n
                warnings.extend(warn)
            if square_groups:
                n, warn = self._square_kind.generate(doc, level, square_groups,
                                                     bundle_dir=_BUNDLE_DIR)
                created += n
                warnings.extend(warn)
            t.Commit()
        except ValueError as ex:
            t.RollBack()
            forms.alert(str(ex), title="Auto Pile")
            return
        except Exception as ex:
            t.RollBack()
            _safe_log("Generate failed: {}".format(ex))
            forms.alert("Failed to generate piles:\n{}".format(ex), title="Auto Pile")
            self.lblStatus.Text = "Failed."
            return

        self._update_family_info()
        if skipped_custom:
            warnings.append(u"Skipped {} custom-shape type(s) (no custom "
                            u"pile family yet): {}".format(
                                len(skipped_custom), u", ".join(skipped_custom)))
        if warnings:
            forms.alert(
                u"Placed {} pile(s) in {} type(s).\n\nSome items had "
                u"problems:\n{}".format(created, groups_total, u"\n".join(warnings)),
                title="Auto Pile")
            self.lblStatus.Text = "Placed {} pile(s), {} warning(s).".format(
                created, len(warnings))
        else:
            self.lblStatus.Text = "Placed {} pile(s) in {} type(s) on {}.".format(
                created, groups_total, get_name(level))

    def on_close(self, sender, e):
        self.Close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if doc is None:
    forms.alert("No active Revit document.", exitscript=True)


def _pick_cad_layers(prompt):
    """Multi-pick: click one or more entities (Enter/right-click
    'Finish' to confirm, Esc to cancel), returning (import_element_id,
    [Category, ...]) -- lets one 'layer' pick sweep up entities split
    across several real CAD layers (common on multi-block-flattened
    DWG imports)."""
    refs = cad_read.pick_points_on_cad_multi(uidoc, prompt)
    if refs is None:
        return None
    try:
        return cad_read.resolve_cad_layers(doc, refs)
    except ValueError as ex:
        if str(ex) == "not_cad":
            forms.alert("The picked element is not part of an imported/linked "
                        "CAD drawing. Pick an entity on the CAD.",
                        title="Auto Pile")
        elif str(ex) == "mixed_imports":
            forms.alert("All picks in one selection must be on the SAME CAD "
                        "import. Pick entities from one import at a time.",
                        title="Auto Pile")
        else:
            _safe_log("_pick_cad_layers failed: {}".format(ex))
            forms.alert("Could not read the layer of a picked object.",
                        title="Auto Pile")
        return None


PICK_PROMPTS = {
    'perimeter': "Pick one or more pile circles/outlines/blocks (Enter to finish, Esc to cancel)",
    'label': "Pick one or more pile/cap labels (Enter to finish, Esc to cancel)",
}

try:
    xaml_file = script.get_bundle_file('ui.xaml')
    # The dialog is modal, so element picking has to happen between dialog
    # sessions: Pick Layer / Pick Label close the window with pick_requested
    # set, we pick here, then reopen with the picked layer selected.
    state = None
    pending_pick = None
    while True:
        window = AutoPileWindow(xaml_file)
        window.initialize_session(state, pending_pick)
        pending_pick = None
        window.ShowDialog()
        if not window.pick_requested:
            break
        state = window.saved_state
        result = _pick_cad_layers(PICK_PROMPTS[window.pick_requested])
        if result is not None:
            pending_pick = (window.pick_requested, result[0], result[1])
except Exception as ex:
    import traceback
    _safe_log("Entry point failed: {}\n{}".format(ex, traceback.format_exc()))
