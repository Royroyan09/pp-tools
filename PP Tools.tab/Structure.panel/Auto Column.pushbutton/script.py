# -*- coding: utf-8 -*-
"""Auto Column

Auto-generate 3D structural columns from an imported/linked DWG: pick
the perimeter layer (circles, rectangles, or steel-profile outlines,
one per column) and the label layer (or check "No labels" to skip
straight to shape-only classification), review one column type per
unique label with material (concrete/steel, editable), size (b x h or
diameter, mm), and — for steel — a snapped standard section
(overridable), fill in Unconnected Height only for types with no level
above, choose a base level and generate. Concrete uses the default
rectangular/round column families; steel snaps to the nearest AISC
section (no SNI library exists on this machine — see ac_sections.py).
Generated columns are auto-joined to nearby structural floors (floor
cuts column) and structural framing (column cuts framing) -- see
ac_join.py; steel-column join is off by default (unconfirmed behavior,
see ac_config.ENABLE_STEEL_COLUMN_JOIN). Custom (L/T/other) profiles
are classified but not yet generated -- see ac_kinds.CustomColumnFamily.

CAD reading, label matching, and placement are shared with Auto
Foundation/Auto Pile via the extension's pp_common lib.
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

from Autodesk.Revit.DB import Options, Transaction

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

import ac_config as cfg
import ac_shapes
import ac_kinds
import ac_sections
import ac_join
from pp_common import cad_read
from pp_common import units as pp_units
from pp_common import logging_util as pp_logging
from pp_common import dxf_text as pp_dxf
from pp_common import levels as pp_levels
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
# Unit helpers (delegated to pp_common)
# ---------------------------------------------------------------------------

def get_name(element):
    return pp_units.get_name(element)


def length_unit_label():
    return pp_units.length_unit_label(doc)


def display_to_internal_length(text):
    return pp_units.display_to_internal_length(doc, text)


def fmt_length(value_ft):
    return pp_units.fmt_length(doc, value_ft)


def fmt_mm(value_ft):
    return pp_units.fmt_num(value_ft / MM_TO_FT)


# ---------------------------------------------------------------------------
# Row view model bound to the WPF grid
# ---------------------------------------------------------------------------

class TypeRow(object):
    def __init__(self, label, count, shape_kind, material, material_note,
                 size1_text, size2_text, section_text, snap_note,
                 unconnected_height_text, note=""):
        self.Include = True
        self.Label = label
        self.Count = count
        self.Shape = shape_kind             # circle | rect | I/H | channel | hollow | custom
        self.Material = material            # 'concrete' | 'steel' | 'UNCLASSIFIED' (editable)
        self.MaterialNote = material_note   # reason, read-only
        self.Size1 = size1_text             # b / diameter (mm)
        self.Size2 = size2_text             # h (blank for round / hollow-square)
        self.Section = section_text         # steel matched/overridable section name
        self.SnapNote = snap_note           # e.g. "snap dist 8.6mm", read-only
        self.UnconnectedHeight = unconnected_height_text  # mm; used only if no level above
        self.Note = note


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class AutoColumnWindow(forms.WPFWindow):

    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)

        self.lblDocTitle.Text = doc.Title or "Untitled"
        self._load_logo()

        self.pick_requested = None
        self.saved_state = None

        self._rect_concrete = ac_kinds.RectConcreteColumnFamily()
        self._round_concrete = ac_kinds.RoundConcreteColumnFamily()
        self._steel = ac_kinds.SteelColumnFamily()
        # label -> [shape], filled by Apply, consumed by Generate
        self._groups = {}
        self._saved_rows = {}

        unit_label = length_unit_label()
        for col, base in ((self.colSize1, "b / Diameter"), (self.colSize2, "h"),
                          (self.colUnconnHeight, "Unconn. Height")):
            col.Header = "{} ({})".format(base, unit_label) if unit_label else base

        self.dgTypes.ItemsSource = ObservableCollection[object]()

        self._load_cad_imports()
        self._load_levels()
        self._update_family_info()

        self.cmbCadImport.SelectionChanged += self.on_cad_import_changed
        self.btnPickPerim.Click += lambda s, e: self._request_pick('perimeter')
        self.btnPickLabel.Click += lambda s, e: self._request_pick('label')
        self.btnPickSteelSymbol.Click += lambda s, e: self._request_pick('steel_symbol')
        self.btnApply.Click += self.on_apply
        self.btnGenerate.Click += self.on_generate
        self.btnClose.Click += self.on_close
        self.chkNoLabels.Checked += self.on_no_labels_toggled
        self.chkNoLabels.Unchecked += self.on_no_labels_toggled
        self.chkSteelSymbols.Checked += self.on_steel_symbols_toggled
        self.chkSteelSymbols.Unchecked += self.on_steel_symbols_toggled
        self.btnBatchFillUnconnHeight.Click += self.on_batch_fill_unconn_height

        self.on_cad_import_changed(None, None)

    # ------------------------------------------------------------------

    def _load_logo(self):
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
        self.cmbSteelSymbolLayer.ItemsSource = [ComboItem(c.Name, LayerSelection([c])) for c in layers]
        self._preselect_by_hints(self.cmbPerimLayer, cfg.PERIMETER_LAYER_HINTS)
        self._preselect_by_hints(self.cmbLabelLayer, cfg.LABEL_LAYER_HINTS)
        self._preselect_by_hints(self.cmbSteelSymbolLayer, cfg.STEEL_SYMBOL_LAYER_HINTS)

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

    def on_steel_symbols_toggled(self, sender, e):
        enabled = self.chkSteelSymbols.IsChecked
        self.cmbSteelSymbolLayer.IsEnabled = enabled
        self.btnPickSteelSymbol.IsEnabled = enabled

    def on_batch_fill_unconn_height(self, sender, e):
        text = (self.txtBatchUnconnHeight.Text or u"").strip()
        if not text:
            forms.alert("Type an Unconnected Height value first.", title="Auto Column")
            return
        selected = list(self.dgTypes.SelectedItems or [])
        if not selected:
            forms.alert("Select one or more rows in the grid first "
                        "(Ctrl/Shift-click), then Fill Selected Rows.",
                        title="Auto Column")
            return
        for row in selected:
            row.UnconnectedHeight = text
        self.dgTypes.Items.Refresh()
        self.lblStatus.Text = "Filled Unconnected Height for {} row(s).".format(len(selected))

    def _load_levels(self):
        levels = pp_levels.list_levels_sorted(doc)
        unit_label = length_unit_label()
        items = []
        for lv in levels:
            disp = u"{}  ({} {})".format(get_name(lv), fmt_length(lv.Elevation), unit_label)
            items.append(ComboItem(disp, lv))
        self.cmbLevel.ItemsSource = items
        if items:
            self.cmbLevel.SelectedIndex = 0

    def _update_family_info(self):
        base_note = ("Columns run from the base level to the next level "
                     "above (unconnected height used only where there is "
                     "none). ")
        parts = []
        for kind in (self._rect_concrete, self._round_concrete):
            try:
                sym = kind.find_base_symbol(doc)
            except Exception:
                sym = None
            if sym is None:
                parts.append("{} not loaded yet (loaded automatically on "
                             "Generate).".format(kind.display_name))
                continue
            params = kind.resolve_params(sym)
            missing = [k for k in kind.size_param_candidates if not params.get(k)]
            if missing:
                parts.append("WARNING: {} family '{}' is loaded but its {} "
                             "parameter(s) could not be identified.".format(
                                 kind.display_name, sym.Family.Name, ", ".join(missing)))
            else:
                parts.append("{} family '{}' OK ({}).".format(
                    kind.display_name.capitalize(), sym.Family.Name,
                    ", ".join("{}={}".format(k, v) for k, v in params.items())))
        steel_counts = []
        for name, catalog in (("W", ac_sections.W_SHAPES), ("C", ac_sections.C_SHAPES),
                              ("HSS-rect", ac_sections.HSS_RECT),
                              ("HSS-sq", ac_sections.HSS_SQUARE),
                              ("HSS-round", ac_sections.HSS_ROUND)):
            steel_counts.append("{}:{}".format(name, catalog.row_count()))
        parts.append("Steel catalogs (AISC, no SNI library found) — " + ", ".join(steel_counts))
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
            'steel_symbol_layer_names': None, 'level_id': None, 'rows': {},
            'no_labels': self.chkNoLabels.IsChecked,
            'steel_symbols': self.chkSteelSymbols.IsChecked,
        }
        if self.cmbCadImport.SelectedItem is not None:
            state['import_id'] = self.cmbCadImport.SelectedItem.Value.Id
        if self.cmbPerimLayer.SelectedItem is not None:
            state['perim_layer_names'] = self.cmbPerimLayer.SelectedItem.Value.names
        if self.cmbLabelLayer.SelectedItem is not None:
            state['label_layer_names'] = self.cmbLabelLayer.SelectedItem.Value.names
        if self.cmbSteelSymbolLayer.SelectedItem is not None:
            state['steel_symbol_layer_names'] = self.cmbSteelSymbolLayer.SelectedItem.Value.names
        if self.cmbLevel.SelectedItem is not None:
            state['level_id'] = self.cmbLevel.SelectedItem.Value.Id
        for row in (self.dgTypes.ItemsSource or []):
            state['rows'][row.Label] = (row.Material, row.Size1, row.Size2,
                                        row.Section, row.UnconnectedHeight)
        return state

    def initialize_session(self, state, pending_pick):
        try:
            if state:
                self._select_import(state.get('import_id'))
                select_layers_by_name(self.cmbPerimLayer, state.get('perim_layer_names'))
                select_layers_by_name(self.cmbLabelLayer, state.get('label_layer_names'))
                select_layers_by_name(self.cmbSteelSymbolLayer, state.get('steel_symbol_layer_names'))
                self._select_level(state.get('level_id'))
                self._saved_rows = state.get('rows') or {}
                self.chkNoLabels.IsChecked = bool(state.get('no_labels'))
                self.on_no_labels_toggled(None, None)
                self.chkSteelSymbols.IsChecked = bool(state.get('steel_symbols'))
                self.on_steel_symbols_toggled(None, None)
            if pending_pick:
                which, import_id, categories = pending_pick
                self._select_import(import_id)
                combo = {'perimeter': self.cmbPerimLayer, 'label': self.cmbLabelLayer,
                        'steel_symbol': self.cmbSteelSymbolLayer}[which]
                set_layer_selection(combo, categories)
                if which == 'steel_symbol':
                    self.chkSteelSymbols.IsChecked = True
                    self.on_steel_symbols_toggled(None, None)
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
                        "(or use Pick Layer).", title="Auto Column")
            return
        if not no_labels and label_item is None:
            forms.alert("Choose a label layer first (or use Pick Label), or "
                        "check 'No labels in this CAD'.", title="Auto Column")
            return

        shapes, report = ac_shapes.scan_perimeter_layer(
            doc, cad_item.Value, perim_item.Value, cfg)

        symbol_note = ""
        if self.chkSteelSymbols.IsChecked:
            symbol_item = self.cmbSteelSymbolLayer.SelectedItem
            if symbol_item is None:
                forms.alert("Choose a steel symbol layer first (or use Pick "
                            "Layer), or uncheck 'Detect footprint-less steel "
                            "columns...'.", title="Auto Column")
                return
            symbol_shapes, symbol_raw_count = ac_shapes.scan_symbol_layer(
                doc, cad_item.Value, symbol_item.Value, cfg, existing_shapes=shapes)
            shapes = shapes + symbol_shapes
            symbol_note = (" {} steel column(s) detected from {} marker(s) on "
                           "symbol layer '{}'.".format(
                               len(symbol_shapes), symbol_raw_count, symbol_item.Display))

        if not shapes:
            self.dgTypes.ItemsSource = ObservableCollection[object]()
            self._groups = {}
            self.lblInfo.Text = (
                "No circles, closed outlines, or steel symbol markers were "
                "found. Pick a different layer, or check the size limits in "
                "ac_config.py.")
            self.lblStatus.Text = "Scan found 0 columns."
            return

        if no_labels:
            matched = 0
            clean_texts = []
            text_source = "none (no-label mode)"
            groups = ac_shapes.group_by_size_only(shapes)
        else:
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

            matched, groups = ac_shapes.match_and_group(shapes, clean_texts, cfg)

        self._groups = groups

        coll = ObservableCollection[object]()
        verdict_counts = {"concrete": 0, "steel": 0, "UNCLASSIFIED": 0}
        for label, shs in groups.items():
            row = self._build_row(label, shs, clean_texts)
            verdict_counts[row.Material] = verdict_counts.get(row.Material, 0) + 1
            saved = self._saved_rows.get(label)
            if saved:
                (row.Material, row.Size1, row.Size2, row.Section,
                 row.UnconnectedHeight) = saved
            coll.Add(row)
        self.dgTypes.ItemsSource = coll
        self._saved_rows = {}

        if no_labels:
            label_note = "no-label mode — grouped purely by shape/size."
        else:
            label_note = "{} labelled from CAD text ({} text entities via {}).".format(
                matched, len(clean_texts), text_source)

        self.lblInfo.Text = (
            "Found {} column(s) in {} type group(s) on layer '{}' "
            "(circle: {circles}, rect: {rect}, I/H: {i_h}, channel: "
            "{channel}, hollow: {hollow}, custom: {custom}); {} Material: "
            "{} concrete, {} steel, {} UNCLASSIFIED.{} Review, then click "
            "Generate.".format(
                len(shapes), coll.Count, perim_item.Display, label_note,
                verdict_counts.get("concrete", 0), verdict_counts.get("steel", 0),
                verdict_counts.get("UNCLASSIFIED", 0), symbol_note, **report))
        self.lblStatus.Text = "Scan complete: {} column(s), {} type(s).".format(
            len(shapes), coll.Count)

    def _build_row(self, label, shapes, clean_texts):
        s0 = shapes[0]
        via_cad_text = any(t[2] == label for t in clean_texts)
        verdict, reason = ac_shapes.reconcile_material(s0, label if via_cad_text else None, cfg)

        unconn_default = fmt_mm(cfg.DEFAULT_UNCONNECTED_HEIGHT_MM * MM_TO_FT)
        notes = []

        if isinstance(s0, ac_shapes.CircleColumnShape):
            d = max(s.diameter_ft for s in shapes)
            d_mm, d_rounded = round_dimension_mm(d / MM_TO_FT)
            d = d_mm * MM_TO_FT
            size1, size2, section, snap_note = fmt_mm(d), "", "", ""
            if d_rounded:
                notes.append("rounded to a clean size")
        elif isinstance(s0, ac_shapes.SymbolicSteelColumnShape):
            # No real footprint to size or rotate from (see the class
            # docstring) -- the user must type the Section by hand.
            size1, size2, section = "", "", ""
            snap_note = "no footprint (symbol only) -- enter Section manually"
        elif s0.display_name == "rect":
            # A mixed group (see the note below) could include a
            # footprint-less symbol shape with no real size -- excluded
            # from the size calculation rather than crashing on it.
            sized = [s for s in shapes if s.width_ft is not None] or shapes
            w = max(s.width_ft for s in sized)
            h = max(s.length_ft for s in sized)
            # Rounding applies here (concrete, no catalog to snap to) but
            # NOT in the steel branch below -- the AISC section snap
            # already finds the nearest REAL standard size, so rounding
            # the raw measurement first would only make that match less
            # accurate, not more.
            w_mm, w_rounded = round_dimension_mm(w / MM_TO_FT)
            h_mm, h_rounded = round_dimension_mm(h / MM_TO_FT)
            w, h = w_mm * MM_TO_FT, h_mm * MM_TO_FT
            size1, size2, section, snap_note = fmt_mm(w), fmt_mm(h), "", ""
            if w_rounded or h_rounded:
                notes.append("rounded to a clean size")
        else:
            sized = [s for s in shapes if s.width_ft is not None] or shapes
            w = max(s.width_ft for s in sized)
            h = max(s.length_ft for s in sized)
            size1, size2 = fmt_mm(w), fmt_mm(h)
            _catalog, matched_row, dist_mm = ac_sections.snap_column_shape(
                s0, w / MM_TO_FT, h / MM_TO_FT)
            if matched_row:
                section = matched_row['name']
                snap_note = "snap dist {:.1f}mm".format(dist_mm)
            else:
                section = ""
                snap_note = "no steel catalog found"
                notes.append("could not load a steel section catalog for this shape")

        if s0.display_name == "custom":
            notes.append("no custom column family yet; generation will skip this type")

        if len(set(s.display_name for s in shapes)) > 1:
            notes.append("mixed shapes under one label; review")

        return TypeRow(label, len(shapes), s0.display_name, verdict, reason,
                      size1, size2, section, snap_note, unconn_default,
                      "; ".join(notes))

    # ------------------------------------------------------------------
    # Generate
    # ------------------------------------------------------------------

    def on_generate(self, sender, e):
        coll = self.dgTypes.ItemsSource
        if coll is None or coll.Count == 0:
            forms.alert("Nothing to generate — pick the layers and click "
                        "Apply first.", title="Auto Column")
            return
        level_item = self.cmbLevel.SelectedItem
        if level_item is None:
            forms.alert("Choose a base level.", title="Auto Column")
            return

        round_groups = []
        rect_groups = []
        steel_groups = []
        skipped_unclassified = []
        skipped_custom = []
        skipped_unsupported = []
        errors = []

        for row in coll:
            if not row.Include:
                continue
            if row.Material == "UNCLASSIFIED":
                skipped_unclassified.append(row.Label)
                continue
            if row.Shape == "custom":
                skipped_custom.append(row.Label)
                continue

            shapes = self._groups.get(row.Label, [])
            if not shapes:
                continue

            height_text = (row.UnconnectedHeight or u"").strip()
            unconn_ft = None
            if height_text:
                try:
                    unconn_ft = display_to_internal_length(height_text)
                except Exception:
                    errors.append(u"'{}': invalid Unconn. Height '{}'.".format(
                        row.Label, row.UnconnectedHeight))
                    continue
                if unconn_ft <= 0:
                    errors.append(u"'{}': Unconn. Height must be greater than "
                                  u"zero.".format(row.Label))
                    continue

            if row.Shape == "circle":
                if row.Material != "concrete":
                    errors.append(u"'{}': a circular column must be concrete "
                                  u"(got '{}').".format(row.Label, row.Material))
                    continue
                try:
                    d_ft = display_to_internal_length(row.Size1)
                except Exception:
                    errors.append(u"'{}': invalid Diameter '{}'.".format(
                        row.Label, row.Size1))
                    continue
                if d_ft <= 0:
                    errors.append(u"'{}': Diameter must be greater than "
                                  u"zero.".format(row.Label))
                    continue
                g = {'label': row.Label, 'diameter_ft': d_ft, 'shapes': shapes}
                if unconn_ft is not None:
                    g['unconnected_height_ft'] = unconn_ft
                round_groups.append(g)
                continue

            if row.Shape == "symbol":
                # No real footprint (see ac_shapes.SymbolicSteelColumnShape)
                # -- Size1/Size2 are intentionally blank, so skip straight
                # to the steel path. Rotation IS available though -- each
                # shape carries its own .rotation read off the marker
                # glyph in CAD (see scan_symbol_layer) -- so the matched
                # catalog row's REAL width/height are looked up here
                # (rather than a placeholder equal width/length) purely
                # so ac_kinds' rotation-axis detection has a genuine
                # non-square aspect ratio to measure against.
                if row.Material != "steel":
                    errors.append(u"'{}': a footprint-less symbol column "
                                  u"must be steel (got '{}').".format(
                                      row.Label, row.Material))
                    continue
                section_name = (row.Section or u"").strip()
                if not section_name:
                    errors.append(u"'{}': choose/confirm a steel Section "
                                  u"first.".format(row.Label))
                    continue
                family_path = ac_sections.W_SHAPES.family_path()
                catalog_row = ac_sections.W_SHAPES.row_by_name(section_name)
                if catalog_row:
                    g_width_ft = catalog_row['width_mm'] * MM_TO_FT
                    g_length_ft = catalog_row['height_mm'] * MM_TO_FT
                else:
                    g_width_ft, g_length_ft = 1.0, 1.0
                g = {'label': row.Label, 'width_ft': g_width_ft, 'length_ft': g_length_ft,
                    'shapes': shapes, 'family_path': family_path,
                    'section_name': section_name}
                if unconn_ft is not None:
                    g['unconnected_height_ft'] = unconn_ft
                steel_groups.append(g)
                continue

            try:
                w_ft = display_to_internal_length(row.Size1)
                h_ft = display_to_internal_length(row.Size2)
            except Exception:
                errors.append(u"'{}': invalid size '{}' x '{}'.".format(
                    row.Label, row.Size1, row.Size2))
                continue
            if w_ft <= 0 or h_ft <= 0:
                errors.append(u"'{}': Width/Depth must be greater than "
                              u"zero.".format(row.Label))
                continue

            if row.Shape == "rect" and row.Material == "concrete":
                g = {'label': row.Label, 'width_ft': w_ft, 'length_ft': h_ft,
                    'shapes': shapes}
                if unconn_ft is not None:
                    g['unconnected_height_ft'] = unconn_ft
                rect_groups.append(g)
                continue

            if row.Shape in ("rect", "I/H", "i_h", "channel", "hollow") and row.Material == "steel":
                # A plain rectangle marked steel by hand (many real drawings
                # draw a Wide Flange column's plan footprint as just its
                # bounding rectangle, with the true H-profile only shown in
                # a separate detail view -- confirmed against a real
                # drawing where this is exactly the case) is snapped
                # against the W-shape catalog, same as a true I/H outline;
                # ac_sections.catalog_for_shape treats 'rect' as W by
                # default. Type the Section by hand first if it should be
                # a different catalog (channel/HSS).
                section_name = (row.Section or u"").strip()
                if not section_name:
                    errors.append(u"'{}': choose/confirm a steel Section "
                                  u"first.".format(row.Label))
                    continue
                w_mm = w_ft / MM_TO_FT
                h_mm = h_ft / MM_TO_FT
                catalog = ac_sections.catalog_for_shape(row.Shape, w_mm, h_mm)
                family_path = catalog.family_path() if catalog else None
                g = {'label': row.Label, 'width_ft': w_ft, 'length_ft': h_ft,
                    'shapes': shapes, 'family_path': family_path,
                    'section_name': section_name}
                if unconn_ft is not None:
                    g['unconnected_height_ft'] = unconn_ft
                steel_groups.append(g)
                continue

            if row.Shape in ("I/H", "i_h", "channel", "hollow") and row.Material != "steel":
                skipped_unsupported.append(
                    u"'{}': a {} profile has no matching concrete "
                    u"family.".format(row.Label, row.Shape))
                continue
            if row.Shape == "rect":
                errors.append(u"'{}': unrecognized Material '{}'.".format(
                    row.Label, row.Material))
                continue

            errors.append(u"'{}': unrecognized shape '{}'.".format(row.Label, row.Shape))

        if errors:
            forms.alert(u"Fix these rows first:\n\n{}".format(u"\n".join(errors)),
                        title="Auto Column")
            return
        if not (round_groups or rect_groups or steel_groups):
            msg = "No rows are checked/ready for generation."
            forms.alert(msg, title="Auto Column")
            return

        level = level_item.Value
        next_above = pp_levels.next_level_above(doc, level)
        unconn_default_ft = cfg.DEFAULT_UNCONNECTED_HEIGHT_MM * MM_TO_FT
        groups_total = len(round_groups) + len(rect_groups) + len(steel_groups)

        t = Transaction(doc, "Auto Column: Generate")
        t.Start()
        try:
            created = 0
            warnings = []
            concrete_instances = []
            steel_instances = []
            if round_groups:
                n, warn, insts = self._round_concrete.generate(
                    doc, level, next_above, unconn_default_ft, round_groups)
                created += n
                warnings.extend(warn)
                concrete_instances.extend(insts)
            if rect_groups:
                n, warn, insts = self._rect_concrete.generate(
                    doc, level, next_above, unconn_default_ft, rect_groups)
                created += n
                warnings.extend(warn)
                concrete_instances.extend(insts)
            if steel_groups:
                n, warn, insts = self._steel.generate(
                    doc, level, next_above, unconn_default_ft, steel_groups)
                created += n
                warnings.extend(warn)
                steel_instances.extend(insts)

            margin_ft = cfg.JOIN_SEARCH_MARGIN_MM * MM_TO_FT
            join_targets = list(concrete_instances)
            if cfg.ENABLE_STEEL_COLUMN_JOIN:
                join_targets.extend(steel_instances)
            joined, join_skipped = ac_join.join_columns_to_floors_and_framing(
                doc, join_targets, margin_ft=margin_ft)

            t.Commit()
        except ValueError as ex:
            t.RollBack()
            forms.alert(str(ex), title="Auto Column")
            return
        except Exception as ex:
            t.RollBack()
            _safe_log("Generate failed: {}".format(ex))
            forms.alert("Failed to generate columns:\n{}".format(ex), title="Auto Column")
            self.lblStatus.Text = "Failed."
            return

        self._update_family_info()
        if skipped_unclassified:
            warnings.append(u"Skipped {} UNCLASSIFIED type(s) (needs a manual "
                            u"material pick): {}".format(
                                len(skipped_unclassified), u", ".join(skipped_unclassified)))
        if skipped_custom:
            warnings.append(u"Skipped {} custom-profile type(s) (no custom "
                            u"column family yet): {}".format(
                                len(skipped_custom), u", ".join(skipped_custom)))
        warnings.extend(skipped_unsupported)

        join_note = u"{} join(s) made, {} skipped (no intersecting floor/framing " \
                   u"nearby -- not an error).".format(joined, join_skipped)
        if not cfg.ENABLE_STEEL_COLUMN_JOIN and steel_instances:
            join_note += u" Steel columns were not join-tested (ENABLE_STEEL_COLUMN_JOIN is off)."

        if warnings:
            forms.alert(
                u"Placed {} column(s) in {} type(s). {}\n\nSome items had "
                u"problems:\n{}".format(created, groups_total, join_note, u"\n".join(warnings)),
                title="Auto Column")
            self.lblStatus.Text = "Placed {} column(s), {} warning(s). {}".format(
                created, len(warnings), join_note)
        else:
            self.lblStatus.Text = "Placed {} column(s) in {} type(s) on {}. {}".format(
                created, groups_total, get_name(level), join_note)

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
                        title="Auto Column")
        elif str(ex) == "mixed_imports":
            forms.alert("All picks in one selection must be on the SAME CAD "
                        "import. Pick entities from one import at a time.",
                        title="Auto Column")
        else:
            _safe_log("_pick_cad_layers failed: {}".format(ex))
            forms.alert("Could not read the layer of a picked object.",
                        title="Auto Column")
        return None


PICK_PROMPTS = {
    'perimeter': "Pick one or more column circles/rectangles/profile outlines (Enter to finish, Esc to cancel)",
    'label': "Pick one or more column labels (Enter to finish, Esc to cancel)",
    'steel_symbol': "Pick one or more steel column marker glyphs (Enter to finish, Esc to cancel)",
}

try:
    xaml_file = script.get_bundle_file('ui.xaml')
    state = None
    pending_pick = None
    while True:
        window = AutoColumnWindow(xaml_file)
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
