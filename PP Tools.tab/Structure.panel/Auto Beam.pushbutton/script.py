# -*- coding: utf-8 -*-
"""Auto Beam

M4: pick the beam layer + mode (as before), optionally a column layer,
and a label layer (or check "No labels" to skip straight to shape-only
naming) -- Apply reports each CONTINUOUS SPAN with its matched label
and a material verdict (concrete/steel/timber) reconciled from the
beam layer's own name (PRIMARY cue) and the label's prefix (SECONDARY
cue). Cues that disagree, or neither being conclusive, come back
UNCLASSIFIED rather than a guess. The beam-table parser, the type-list
UI, Generate, and the auto-join pass land in the milestones that follow.

CAD reading, label matching, and the open-line/collinearity geometry
(Segment, pair_parallel_lines, is_collinear) are shared with Auto
Foundation/Auto Pile/Auto Column via the extension's pp_common lib.
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

import ab_config as cfg
import ab_shapes
import ab_kinds
import ab_sections
import ab_join
from pp_common import cad_read
from pp_common import units as pp_units
from pp_common import levels as pp_levels
from pp_common import logging_util as pp_logging
from pp_common import dxf_text as pp_dxf
from pp_common.config_base import MM_TO_FT
from pp_common.wpf_helpers import (
    ComboItem, LayerSelection, set_layer_selection, select_layers_by_name)

MATERIAL_OPTIONS = ["concrete", "steel", "timber", "UNCLASSIFIED"]


def _safe_log(msg):
    try:
        log_path = script.get_bundle_file('error.log')
        pp_logging.safe_log(log_path, msg)
    except Exception:
        pass


def get_name(element):
    return pp_units.get_name(element)


def fmt_mm(value_ft):
    return pp_units.fmt_num(value_ft / MM_TO_FT)


def length_unit_label():
    return pp_units.length_unit_label(doc)


def fmt_length(value_ft):
    return pp_units.fmt_length(doc, value_ft)


def mm_to_ft(mm_text):
    """Parses a plain millimetre string (as shown in the BEAM TYPES
    grid's b (mm)/h (mm) columns) into feet. Raises ValueError on
    anything non-numeric -- callers turn that into a per-row error."""
    return float(mm_text) * MM_TO_FT


# ---------------------------------------------------------------------------
# Row view model bound to the report grid
# ---------------------------------------------------------------------------

class CenterlineRow(object):
    def __init__(self, index, label, material, material_note, sources,
                 start_text, end_text, length_text, width_text, note=""):
        self.Index = index
        self.Label = label
        self.Material = material        # 'concrete' | 'steel' | 'timber' | 'UNCLASSIFIED'
        self.MaterialNote = material_note
        self.Sources = sources          # e.g. "3" or "3+7" (1-based, pre-stitch)
        self.StartText = start_text
        self.EndText = end_text
        self.LengthText = length_text
        self.WidthText = width_text
        self.Note = note


class MergeRow(object):
    def __init__(self, pair, reason, gap_text, column_text):
        self.Pair = pair
        self.Reason = reason
        self.GapText = gap_text
        self.ColumnText = column_text


class TableRow(object):
    def __init__(self, label, b_text, h_text, note=""):
        self.Label = label
        self.BText = b_text
        self.HText = h_text
        self.Note = note


class BeamTypeRow(object):
    def __init__(self, label, count, material, material_note,
                 b_text, h_text, size_note, section="", note=""):
        self.Include = True
        self.Label = label
        self.Count = count
        self.Material = material        # 'concrete' | 'steel' | 'timber' | 'UNCLASSIFIED' (dropdown, editable)
        self.MaterialNote = material_note
        self.BText = b_text             # mm, editable
        self.HText = h_text             # mm, editable
        self.SizeNote = size_note       # where b/h came from, read-only
        self.Section = section          # steel/timber profile name, editable
        self.Note = note


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class AutoBeamWindow(forms.WPFWindow):

    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)

        self.lblDocTitle.Text = doc.Title or "Untitled"
        self._load_logo()

        self._concrete_beam = ab_kinds.ConcreteBeamFamily()
        self._steel_beam = ab_kinds.SteelBeamFamily()
        self._timber_beam = ab_kinds.TimberBeamFamily()

        self.pick_requested = None
        self.saved_state = None
        # (min_xy, max_xy) in feet from the last "Pick Table Region",
        # round-tripped through capture_state/initialize_session same
        # as every other pick in this dialog
        self.table_region = None
        # label -> [BeamSpanShape], filled by Apply, consumed by Generate (M7)
        self._groups = {}
        # label -> (Include, Material, BText, HText, Section), carried
        # across an Apply re-run the same way Auto Column preserves
        # hand-edited type rows across a re-pick
        self._saved_type_rows = {}

        self.dgCenterlines.ItemsSource = ObservableCollection[object]()
        self.dgMerges.ItemsSource = ObservableCollection[object]()
        self.dgTable.ItemsSource = ObservableCollection[object]()
        self.dgTypes.ItemsSource = ObservableCollection[object]()
        self.colTypeMaterial.ItemsSource = list(MATERIAL_OPTIONS)

        self._load_cad_imports()
        self._load_levels()
        self._update_family_info()

        self.cmbCadImport.SelectionChanged += self.on_cad_import_changed
        self.btnPickLayer.Click += lambda s, e: self._request_pick('beam_layer')
        self.btnPickColumnLayer.Click += lambda s, e: self._request_pick('column_layer')
        self.btnPickLabel.Click += lambda s, e: self._request_pick('label_layer')
        self.btnPickTableRegion.Click += lambda s, e: self._request_pick('table_region')
        self.btnApply.Click += self.on_apply
        self.btnGenerate.Click += self.on_generate
        self.btnBatchFillH.Click += self.on_batch_fill_h
        self.btnClose.Click += self.on_close
        self.chkNoLabels.Checked += self.on_no_labels_toggled
        self.chkNoLabels.Unchecked += self.on_no_labels_toggled
        self.radTableYes.Checked += self.on_table_toggled
        self.radTableNo.Checked += self.on_table_toggled

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
        self.cmbBeamLayer.ItemsSource = None
        self.cmbColumnLayer.ItemsSource = None
        self.cmbLabelLayer.ItemsSource = None
        if item is None:
            return
        layers = cad_read.list_layers(item.Value)
        self.cmbBeamLayer.ItemsSource = [ComboItem(c.Name, LayerSelection([c])) for c in layers]
        self.cmbColumnLayer.ItemsSource = [ComboItem(c.Name, LayerSelection([c])) for c in layers]
        self.cmbLabelLayer.ItemsSource = [ComboItem(c.Name, LayerSelection([c])) for c in layers]
        self._preselect_by_hints(self.cmbBeamLayer, cfg.BEAM_LAYER_HINTS)
        self._preselect_by_hints(self.cmbColumnLayer, cfg.COLUMN_LAYER_HINTS)
        self._preselect_by_hints(self.cmbLabelLayer, cfg.LABEL_LAYER_HINTS)

    def _preselect_by_hints(self, combo, hints):
        for item in (combo.ItemsSource or []):
            upper = item.Display.upper()
            for hint in hints:
                if hint.upper() in upper:
                    combo.SelectedItem = item
                    return

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

    def _select_level(self, level_id):
        if level_id is None:
            return
        for item in (self.cmbLevel.ItemsSource or []):
            if item.Value.Id == level_id:
                self.cmbLevel.SelectedItem = item
                return

    def _update_family_info(self):
        base_note = ("Beams run with their TOP flush to the selected level "
                     "(Z-justification Top). ")
        parts = []
        try:
            sym = self._concrete_beam.find_base_symbol(doc)
        except Exception:
            sym = None
        if sym is None:
            parts.append("{} not loaded yet (loaded automatically on "
                         "Generate).".format(self._concrete_beam.display_name))
        else:
            params = self._concrete_beam.resolve_params(sym)
            missing = [k for k in ('width', 'depth') if not params.get(k)]
            if missing:
                parts.append("WARNING: {} family '{}' is loaded but its {} "
                             "parameter(s) could not be identified.".format(
                                 self._concrete_beam.display_name, sym.Family.Name,
                                 ", ".join(missing)))
            else:
                parts.append("{} family '{}' OK ({}).".format(
                    self._concrete_beam.display_name.capitalize(), sym.Family.Name,
                    ", ".join("{}={}".format(k, v) for k, v in params.items())))
        steel_counts = []
        for name, catalog in (("W", ab_sections.W_SHAPES), ("C", ab_sections.C_SHAPES),
                              ("HSS-rect", ab_sections.HSS_RECT)):
            steel_counts.append("{}:{}".format(name, catalog.row_count()))
        parts.append("Steel catalogs (AISC, no SNI library found) — " + ", ".join(steel_counts))
        timber_rows = ab_sections.TIMBER.row_count()
        parts.append("Timber catalog (M_Timber) — {} section(s){}.".format(
            timber_rows, "" if timber_rows else " NOT FOUND"))
        self.lblFamilyInfo.Text = base_note + " ".join(parts)

    def on_no_labels_toggled(self, sender, e):
        no_labels = self.chkNoLabels.IsChecked
        self.cmbLabelLayer.IsEnabled = not no_labels
        self.btnPickLabel.IsEnabled = not no_labels
        self.lblLabelLayer.IsEnabled = not no_labels

    def on_table_toggled(self, sender, e):
        self.btnPickTableRegion.IsEnabled = bool(self.radTableYes.IsChecked)

    def on_batch_fill_h(self, sender, e):
        text = (self.txtBatchH.Text or u"").strip()
        if not text:
            forms.alert("Type an h (mm) value first.", title="Auto Beam")
            return
        selected = list(self.dgTypes.SelectedItems or [])
        if not selected:
            forms.alert("Select one or more rows in the BEAM TYPES grid first "
                        "(Ctrl/Shift-click), then Fill Selected Rows.",
                        title="Auto Beam")
            return
        for row in selected:
            row.HText = text
        self.dgTypes.Items.Refresh()
        self.lblStatus.Text = "Filled h for {} row(s).".format(len(selected))

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
            'import_id': None, 'beam_layer_names': None, 'column_layer_names': None,
            'label_layer_names': None, 'no_labels': self.chkNoLabels.IsChecked,
            'mode': '2-line' if self.radTwoLine.IsChecked else '1-line',
            'table_yes': bool(self.radTableYes.IsChecked),
            'table_region': self.table_region,
            'level_id': None, 'type_rows': {},
        }
        if self.cmbCadImport.SelectedItem is not None:
            state['import_id'] = self.cmbCadImport.SelectedItem.Value.Id
        if self.cmbBeamLayer.SelectedItem is not None:
            state['beam_layer_names'] = self.cmbBeamLayer.SelectedItem.Value.names
        if self.cmbColumnLayer.SelectedItem is not None:
            state['column_layer_names'] = self.cmbColumnLayer.SelectedItem.Value.names
        if self.cmbLabelLayer.SelectedItem is not None:
            state['label_layer_names'] = self.cmbLabelLayer.SelectedItem.Value.names
        if self.cmbLevel.SelectedItem is not None:
            state['level_id'] = self.cmbLevel.SelectedItem.Value.Id
        for row in (self.dgTypes.ItemsSource or []):
            state['type_rows'][row.Label] = (row.Include, row.Material, row.BText,
                                             row.HText, row.Section)
        return state

    def initialize_session(self, state, pending_pick):
        try:
            if state:
                self._select_import(state.get('import_id'))
                select_layers_by_name(self.cmbBeamLayer, state.get('beam_layer_names'))
                select_layers_by_name(self.cmbColumnLayer, state.get('column_layer_names'))
                select_layers_by_name(self.cmbLabelLayer, state.get('label_layer_names'))
                if state.get('mode') == '1-line':
                    self.radOneLine.IsChecked = True
                else:
                    self.radTwoLine.IsChecked = True
                self.chkNoLabels.IsChecked = bool(state.get('no_labels'))
                self.on_no_labels_toggled(None, None)
                self._select_level(state.get('level_id'))
                self._saved_type_rows = state.get('type_rows') or {}
                self.table_region = state.get('table_region')
                if state.get('table_yes') or self.table_region is not None:
                    self.radTableYes.IsChecked = True
                self.on_table_toggled(None, None)
            if pending_pick and pending_pick[0] == 'table_region':
                _which, min_xy, max_xy = pending_pick
                self.table_region = (min_xy, max_xy)
                self.radTableYes.IsChecked = True
                self.on_table_toggled(None, None)
            elif pending_pick:
                which, import_id, categories = pending_pick
                self._select_import(import_id)
                combo = {'beam_layer': self.cmbBeamLayer, 'column_layer': self.cmbColumnLayer,
                        'label_layer': self.cmbLabelLayer}[which]
                set_layer_selection(combo, categories)
            if self.table_region is not None:
                self._apply_table_region()
            label_ready = self.chkNoLabels.IsChecked or self.cmbLabelLayer.SelectedItem is not None
            if (self.cmbBeamLayer.SelectedItem is not None and label_ready
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

    # ------------------------------------------------------------------
    # Beam table (parse the windowed region, report for verification)
    # ------------------------------------------------------------------

    def _apply_table_region(self):
        if self.table_region is None:
            return
        min_xy, max_xy = self.table_region
        try:
            texts = ab_shapes.collect_texts_in_region(doc, min_xy, max_xy)
        except Exception as ex:
            _safe_log("collect_texts_in_region failed: {}".format(ex))
            self.lblTableInfo.Text = (
                "BEAM TABLE: could not read CAD text for the picked region "
                "({}). Type the table in by hand below (Label/b/h), or try "
                "again after exploding a copy of the drawing so the text "
                "becomes native TextNotes.".format(ex))
            self.dgTable.ItemsSource = ObservableCollection[object]()
            return

        table, report = ab_shapes.parse_beam_table(texts, cfg)

        coll = ObservableCollection[object]()
        for label, (b_mm, h_mm) in table.items():
            note = "conflict: a later row disagreed; first value kept" \
                if label in report['conflicts'] else ""
            coll.Add(TableRow(label, pp_units.fmt_num(b_mm), pp_units.fmt_num(h_mm), note))
        self.dgTable.ItemsSource = coll

        if not texts:
            self.lblTableInfo.Text = (
                "BEAM TABLE: no CAD text was found in the picked region. "
                "Pick a tighter/looser window around the schedule, or type "
                "the table in by hand below (Label/b/h) -- Revit's geometry "
                "API and the DXF export fallback both returned nothing here.")
        elif report['label_col'] is None or report['b_col'] is None or report['h_col'] is None:
            missing = []
            if report['label_col'] is None:
                missing.append("label")
            if report['b_col'] is None:
                missing.append("b")
            if report['h_col'] is None:
                missing.append("h")
            self.lblTableInfo.Text = (
                "BEAM TABLE: found {} row(s) x {} column(s) of text, but "
                "could not identify the {} column(s) from the header -- "
                "check TABLE_*_HEADER_HINTS in ab_config.py, or fill in the "
                "table by hand below.".format(
                    report['n_rows'], report['n_cols'], "/".join(missing)))
        else:
            note = ""
            if report['conflicts']:
                note += " {} label(s) had conflicting rows (first value kept, see Note column).".format(
                    len(report['conflicts']))
            if report['unparsed_rows']:
                note += " {} row(s) had a non-numeric b or h and were skipped.".format(
                    len(report['unparsed_rows']))
            self.lblTableInfo.Text = (
                "BEAM TABLE: parsed {} row(s) x {} column(s) -> {} label(s). "
                "Verify against the drawing below (every cell is editable) "
                "before it's used to auto-fill b/h.{}".format(
                    report['n_rows'], report['n_cols'], len(table), note))

    def _current_table_dict(self):
        """Reads the BEAM TABLE grid as it stands right now (including
        any hand-edits the user made after the parse) into a plain
        {label: (b_mm, h_mm)} dict for the type-list's b/h auto-fill --
        the table is verified/corrected in place (M5), so the type list
        must read the live grid, not the original parse."""
        result = {}
        for row in (self.dgTable.ItemsSource or []):
            label = (row.Label or u"").strip()
            if not label:
                continue
            b_mm = ab_shapes.parse_number_mm(row.BText)
            h_mm = ab_shapes.parse_number_mm(row.HText)
            if b_mm is not None and h_mm is not None:
                result[label] = (b_mm, h_mm)
        return result

    # ------------------------------------------------------------------
    # Scan (Apply)
    # ------------------------------------------------------------------

    def on_apply(self, sender, e):
        cad_item = self.cmbCadImport.SelectedItem
        beam_item = self.cmbBeamLayer.SelectedItem
        column_item = self.cmbColumnLayer.SelectedItem
        no_labels = self.chkNoLabels.IsChecked
        label_item = self.cmbLabelLayer.SelectedItem
        if cad_item is None or beam_item is None:
            forms.alert("Choose a CAD import and a beam layer first "
                        "(or use Pick Layer).", title="Auto Beam")
            return
        if not no_labels and label_item is None:
            forms.alert("Choose a label layer first (or use Pick Label), or "
                        "check 'No labels in this CAD'.", title="Auto Beam")
            return

        mode = '2-line' if self.radTwoLine.IsChecked else '1-line'
        try:
            centerlines, report = ab_shapes.scan_beam_layer(
                doc, cad_item.Value, beam_item.Value, mode, cfg)
        except Exception as ex:
            _safe_log("scan_beam_layer failed: {}".format(ex))
            forms.alert("Could not read geometry from this CAD import:\n{}".format(ex),
                        title="Auto Beam")
            return

        if not centerlines:
            self.dgCenterlines.ItemsSource = ObservableCollection[object]()
            self.dgMerges.ItemsSource = ObservableCollection[object]()
            self.dgTypes.ItemsSource = ObservableCollection[object]()
            self._groups = {}
            self.lblInfo.Text = (
                "No {} were found on layer '{}' (checked {} raw lines and "
                "{} raw polylines; {} segments passed the minimum-length "
                "filter). Pick a different layer, or check "
                "MIN_SEGMENT_LENGTH_MM in ab_config.py.".format(
                    "parallel edge pairs" if mode == '2-line' else "centerlines",
                    beam_item.Display, report['raw_lines'], report['raw_polylines'],
                    report['segments_after_filter']))
            self.lblStatus.Text = "Scan found 0 centerlines."
            return

        column_footprints = []
        if column_item is not None:
            try:
                column_footprints = ab_shapes.scan_column_footprints(
                    doc, cad_item.Value, column_item.Value, cfg)
            except Exception as ex:
                _safe_log("scan_column_footprints failed: {}".format(ex))

        spans, merges = ab_shapes.stitch_continuous(centerlines, column_footprints, cfg)

        if no_labels:
            matched = 0
            clean_texts = []
            text_source = "none (no-label mode)"
            groups = ab_shapes.group_spans_by_size_only(spans)
        else:
            geom = cad_item.Value.get_Geometry(Options())
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

            matched, groups = ab_shapes.match_and_group_spans(spans, clean_texts, cfg)

        span_coll = ObservableCollection[object]()
        idx = 0
        verdict_counts = {"concrete": 0, "steel": 0, "timber": 0, "UNCLASSIFIED": 0}
        for label, shapes_in_group in groups.items():
            for shape in shapes_in_group:
                idx += 1
                via_cad_text = any(t[2] == label for t in clean_texts)
                verdict, reason = ab_shapes.reconcile_material(
                    beam_item.Display, label if via_cad_text else None, cfg)
                verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

                seg = shape.segment
                span = shape.span
                sources = "+".join(str(i) for i in span['source_indices'])
                start_text = "{}, {}".format(fmt_mm(seg.p0[0]), fmt_mm(seg.p0[1]))
                end_text = "{}, {}".format(fmt_mm(seg.p1[0]), fmt_mm(seg.p1[1]))
                length_text = fmt_mm(seg.length)
                width_text = fmt_mm(shape.width_ft) if shape.width_ft is not None else ""
                note = "stitched from {} segment(s)".format(len(span['source_indices'])) \
                    if len(span['source_indices']) > 1 else ""
                if shape.width_varied:
                    note = (note + "; " if note else "") + "width varied across merged segments; largest used"
                span_coll.Add(CenterlineRow(idx, label, verdict, reason, sources,
                                            start_text, end_text, length_text,
                                            width_text, note))
        self.dgCenterlines.ItemsSource = span_coll

        merge_coll = ObservableCollection[object]()
        for m in merges:
            pair_text = "{}+{}".format(m['a_index'], m['b_index'])
            gap_text = fmt_mm(m['gap_ft'])
            col_text = ("{}, {}".format(fmt_mm(m['column_center'][0]), fmt_mm(m['column_center'][1]))
                       if m['column_center'] is not None else "")
            merge_coll.Add(MergeRow(pair_text, m['reason'], gap_text, col_text))
        self.dgMerges.ItemsSource = merge_coll

        self._groups = groups
        table_dict = self._current_table_dict()
        type_coll = ObservableCollection[object]()
        for label, shapes_in_group in groups.items():
            via_cad_text = any(t[2] == label for t in clean_texts)
            verdict, reason = ab_shapes.reconcile_material(
                beam_item.Display, label if via_cad_text else None, cfg)
            b_mm, h_mm, size_note = ab_shapes.resolve_type_sizing(
                label, shapes_in_group, table_dict, cfg)
            row = BeamTypeRow(
                label, len(shapes_in_group), verdict, reason,
                pp_units.fmt_num(b_mm) if b_mm is not None else "",
                pp_units.fmt_num(h_mm) if h_mm is not None else "",
                size_note)
            saved = self._saved_type_rows.get(label)
            if saved:
                (row.Include, row.Material, row.BText, row.HText, row.Section) = saved
            type_coll.Add(row)
        self.dgTypes.ItemsSource = type_coll
        self._saved_type_rows = {}

        if mode == '2-line':
            mode_note = "{} pair(s) formed a centerline + width; {} raw segment(s) could not be paired.".format(
                report['paired'], report['unpaired'])
        else:
            mode_note = "{} segment(s) used directly as centerlines (1-line mode).".format(
                report['segments_after_filter'])
        column_note = (" Column layer '{}': {} footprint(s) available to bridge gaps.".format(
            column_item.Display, len(column_footprints)) if column_item is not None
            else " No column layer picked -- only small-gap stitching applies.")

        if no_labels:
            label_note = "no-label mode — grouped purely by shape/size."
        else:
            label_note = "{} labelled from CAD text ({} text entities via {}).".format(
                matched, len(clean_texts), text_source)

        self.lblInfo.Text = (
            "Layer '{}': {} raw line(s), {} raw polyline(s) ({} segment(s) "
            "after the minimum-length filter). {}{} {} merge(s) made -> {} "
            "continuous span(s) in {} type(s). {} Material: {} concrete, {} "
            "steel, {} timber, {} UNCLASSIFIED. Review the BEAM TYPES grid "
            "below -- b/h auto-filled from the beam table where the label "
            "matches, else from the 2-line gap (h still manual), else fully "
            "manual.".format(
                beam_item.Display, report['raw_lines'], report['raw_polylines'],
                report['segments_after_filter'], mode_note, column_note,
                len(merges), len(spans), type_coll.Count, label_note,
                verdict_counts.get("concrete", 0), verdict_counts.get("steel", 0),
                verdict_counts.get("timber", 0), verdict_counts.get("UNCLASSIFIED", 0)))
        self.lblStatus.Text = "Scan complete: {} centerline(s) -> {} span(s), {} type(s).".format(
            len(centerlines), len(spans), type_coll.Count)

    # ------------------------------------------------------------------
    # Generate
    # ------------------------------------------------------------------

    def on_generate(self, sender, e):
        coll = self.dgTypes.ItemsSource
        if coll is None or coll.Count == 0:
            forms.alert("Nothing to generate — pick the layers and click "
                        "Apply first.", title="Auto Beam")
            return
        level_item = self.cmbLevel.SelectedItem
        if level_item is None:
            forms.alert("Choose a level.", title="Auto Beam")
            return

        concrete_groups = []
        steel_groups = []
        timber_groups = []
        skipped_unclassified = []
        errors = []

        for row in coll:
            if not row.Include:
                continue
            shapes = self._groups.get(row.Label, [])
            if not shapes:
                continue

            if row.Material == "UNCLASSIFIED":
                skipped_unclassified.append(row.Label)
                continue

            if row.Material == "steel":
                section_name = (row.Section or u"").strip()
                b_mm = ab_shapes.parse_number_mm(row.BText)
                h_mm = ab_shapes.parse_number_mm(row.HText)
                if not section_name:
                    if b_mm is None or h_mm is None:
                        errors.append(u"'{}': choose/confirm a steel Section, "
                                      u"or fill in b/h so one can be snapped.".format(
                                          row.Label))
                        continue
                    catalog, matched_row, _dist = ab_sections.snap_beam_size(
                        row.Label, b_mm, h_mm)
                    if not matched_row:
                        errors.append(u"'{}': could not snap a steel section "
                                      u"from b={}/h={} (no catalog found -- see "
                                      u"BEAM_STEEL_LIBRARY_ROOTS in ab_config.py). "
                                      u"Type a Section by hand.".format(
                                          row.Label, row.BText, row.HText))
                        continue
                    section_name = matched_row['name']
                catalog = ab_sections.catalog_for_label(row.Label)
                family_path = catalog.family_path() if catalog else None
                steel_groups.append({
                    'label': row.Label, 'shapes': shapes,
                    'family_path': family_path, 'section_name': section_name,
                })
                continue

            if row.Material == "timber":
                section_name = (row.Section or u"").strip()
                b_mm = ab_shapes.parse_number_mm(row.BText)
                h_mm = ab_shapes.parse_number_mm(row.HText)
                if not section_name:
                    if b_mm is None or h_mm is None:
                        errors.append(u"'{}': choose/confirm a timber Section, "
                                      u"or fill in b/h so one can be snapped.".format(
                                          row.Label))
                        continue
                    matched_row, _dist = ab_sections.snap_timber_size(b_mm, h_mm)
                    if not matched_row:
                        errors.append(u"'{}': could not snap a timber section "
                                      u"from b={}/h={} (no catalog found -- see "
                                      u"TIMBER_LIBRARY_ROOTS in ab_config.py). "
                                      u"Type a Section by hand.".format(
                                          row.Label, row.BText, row.HText))
                        continue
                    section_name = matched_row['name']
                timber_groups.append({
                    'label': row.Label, 'shapes': shapes,
                    'family_path': ab_sections.TIMBER.family_path(), 'section_name': section_name,
                })
                continue

            # concrete
            try:
                w_ft = mm_to_ft(row.BText)
                h_ft = mm_to_ft(row.HText)
            except (ValueError, TypeError):
                errors.append(u"'{}': invalid/missing b or h ('{}' x '{}'). "
                              u"Enter both in mm.".format(
                                  row.Label, row.BText, row.HText))
                continue
            if w_ft <= 0 or h_ft <= 0:
                errors.append(u"'{}': b and h must be greater than zero.".format(row.Label))
                continue
            concrete_groups.append({
                'label': row.Label, 'width_ft': w_ft, 'depth_ft': h_ft, 'shapes': shapes,
            })

        if errors:
            forms.alert(u"Fix these rows first:\n\n{}".format(u"\n".join(errors)),
                        title="Auto Beam")
            return
        if not (concrete_groups or steel_groups or timber_groups):
            forms.alert("No rows are checked/ready for generation.", title="Auto Beam")
            return

        level = level_item.Value
        groups_total = len(concrete_groups) + len(steel_groups) + len(timber_groups)

        t = Transaction(doc, "Auto Beam: Generate")
        t.Start()
        try:
            created = 0
            warnings = []
            concrete_instances = []
            steel_instances = []
            timber_instances = []
            if concrete_groups:
                n, warn, insts = self._concrete_beam.generate(doc, level, concrete_groups)
                created += n
                warnings.extend(warn)
                concrete_instances.extend(insts)
            if steel_groups:
                n, warn, insts = self._steel_beam.generate(doc, level, steel_groups)
                created += n
                warnings.extend(warn)
                steel_instances.extend(insts)
            if timber_groups:
                n, warn, insts = self._timber_beam.generate(doc, level, timber_groups)
                created += n
                warnings.extend(warn)
                timber_instances.extend(insts)

            margin_ft = cfg.JOIN_SEARCH_MARGIN_MM * MM_TO_FT
            joined, join_skipped = ab_join.join_beams_to_columns_and_floors(
                doc, concrete_instances, steel_instances, timber_instances, cfg, margin_ft=margin_ft)

            t.Commit()
        except ValueError as ex:
            t.RollBack()
            forms.alert(str(ex), title="Auto Beam")
            return
        except Exception as ex:
            t.RollBack()
            _safe_log("Generate failed: {}".format(ex))
            forms.alert("Failed to generate beams:\n{}".format(ex), title="Auto Beam")
            self.lblStatus.Text = "Failed."
            return

        self._update_family_info()
        if skipped_unclassified:
            warnings.append(u"Skipped {} UNCLASSIFIED type(s) (needs a manual "
                            u"material pick): {}".format(
                                len(skipped_unclassified), u", ".join(skipped_unclassified)))

        join_note = u"{} join(s) made, {} skipped (no intersecting column/floor " \
                   u"nearby -- not an error).".format(joined, join_skipped)
        if not cfg.ENABLE_STEEL_BEAM_JOIN and steel_instances:
            join_note += u" Steel beams were not join-tested (ENABLE_STEEL_BEAM_JOIN is off)."
        if not cfg.ENABLE_TIMBER_BEAM_JOIN and timber_instances:
            join_note += u" Timber beams were not join-tested (ENABLE_TIMBER_BEAM_JOIN is off)."
        if not cfg.ENABLE_BEAM_BEAM_JOIN:
            join_note += u" Beam-to-beam join is off (ENABLE_BEAM_BEAM_JOIN)."

        if warnings:
            forms.alert(
                u"Placed {} beam(s) in {} type(s). {}\n\nSome items had "
                u"problems:\n{}".format(created, groups_total, join_note, u"\n".join(warnings)),
                title="Auto Beam")
            self.lblStatus.Text = "Placed {} beam(s), {} warning(s). {}".format(
                created, len(warnings), join_note)
        else:
            self.lblStatus.Text = "Placed {} beam(s) in {} type(s) on {}. {}".format(
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
    DWG imports where the beam/label layer got split into near-
    duplicate sub-layers)."""
    refs = cad_read.pick_points_on_cad_multi(uidoc, prompt)
    if refs is None:
        return None
    try:
        return cad_read.resolve_cad_layers(doc, refs)
    except ValueError as ex:
        if str(ex) == "not_cad":
            forms.alert("The picked element is not part of an imported/linked "
                        "CAD drawing. Pick an entity on the CAD.",
                        title="Auto Beam")
        elif str(ex) == "mixed_imports":
            forms.alert("All picks in one selection must be on the SAME CAD "
                        "import. Pick entities from one import at a time.",
                        title="Auto Beam")
        else:
            _safe_log("_pick_cad_layers failed: {}".format(ex))
            forms.alert("Could not read the layer of a picked object.",
                        title="Auto Beam")
        return None


PICK_PROMPTS = {
    'beam_layer': "Pick one or more beam edge/centerline lines (Enter to finish, Esc to cancel)",
    'column_layer': "Pick one or more column outlines (Enter to finish, Esc to cancel)",
    'label_layer': "Pick one or more beam label texts (Enter to finish, Esc to cancel)",
    'table_region': "Window/crossing-select the beam schedule table (Esc to cancel)",
}

try:
    xaml_file = script.get_bundle_file('ui.xaml')
    state = None
    pending_pick = None
    while True:
        window = AutoBeamWindow(xaml_file)
        window.initialize_session(state, pending_pick)
        pending_pick = None
        window.ShowDialog()
        if not window.pick_requested:
            break
        state = window.saved_state
        which = window.pick_requested
        if which == 'table_region':
            region = cad_read.pick_region(uidoc, PICK_PROMPTS[which])
            if region is not None:
                pending_pick = (which, region[0], region[1])
        else:
            result = _pick_cad_layers(PICK_PROMPTS[which])
            if result is not None:
                pending_pick = (which, result[0], result[1])
except Exception as ex:
    import traceback
    _safe_log("Entry point failed: {}\n{}".format(ex, traceback.format_exc()))
