# -*- coding: utf-8 -*-
"""Define Grid

Create Revit grids by typing names and spacing. Supports orthogonal (X/Y)
grids, radial grids (spokes + rings), fully custom straight grids, and
auto-generating grids from a layer in an imported/linked CAD drawing.
"""
from __future__ import print_function

import math

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from System.IO import FileStream, FileMode, FileAccess
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Collections.Generic import List as NetList
from System.Collections.ObjectModel import ObservableCollection

from Autodesk.Revit.DB import (
    FilteredElementCollector, Element, Transaction, Grid, Line, Arc, XYZ,
    SpecTypeId, UnitUtils, ImportInstance, Options, GeometryInstance,
    ElementId
)

from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()


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


DEFAULT_BAY_SPACING_FT = 20.0     # ~6.1 m; prefilled when adding X/Y rows
DEFAULT_ANGLE_SPACING_DEG = 30.0  # prefilled when adding spoke rows
DEFAULT_RING_SPACING_FT = 20.0    # prefilled when adding ring rows
DEFAULT_SPOKE_LENGTH_FT = 40.0    # ~12 m; initial value of the spoke-length field
EXTENT_MARGIN_FT = 5.0            # how far grid lines overshoot the outer grid
FALLBACK_HALF_LEN_FT = 20.0       # used when a direction has no perpendicular grids


def next_numeric_name(n):
    return str(n)


def next_alpha_name(n):
    # 1 -> A, 2 -> B, ... 26 -> Z, 27 -> AA, 28 -> AB ...
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


# ---------------------------------------------------------------------------
# Row view models bound to the WPF grids
# ---------------------------------------------------------------------------

class ComboItem(object):
    def __init__(self, display, value):
        self.Display = display
        self.Value = value


class SpacingRow(object):
    """Used for X/Y orthogonal rows, spokes, and rings: Name + a single
    'spacing from previous' text value (length or angle depending on use)."""
    def __init__(self, index, name, spacing_text):
        self.Index = index
        self.Name = name
        self.Spacing = spacing_text


class CustomRow(object):
    def __init__(self, index, name, sx, sy, ex, ey):
        self.Index = index
        self.Name = name
        self.StartX = sx
        self.StartY = sy
        self.EndX = ex
        self.EndY = ey


class CadPreviewRow(object):
    def __init__(self, name, start_pt_ft, end_pt_ft, source):
        self.Name = name
        self.Include = True
        self.Source = source
        self._start_ft = start_pt_ft
        self._end_ft = end_pt_ft
        self.StartX = fmt_length(start_pt_ft.X)
        self.StartY = fmt_length(start_pt_ft.Y)
        self.EndX = fmt_length(end_pt_ft.X)
        self.EndY = fmt_length(end_pt_ft.Y)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class DefineGridWindow(forms.WPFWindow):

    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)

        self.lblDocTitle.Text = doc.Title or "Untitled"
        self._load_logo()

        unit_label = length_unit_label()
        length_header = "Spacing ({})".format(unit_label) if unit_label else "Spacing"
        self.colOrthoXSpacing.Header = length_header
        self.colOrthoYSpacing.Header = length_header
        self.colRingSpacing.Header = length_header
        coord_header_x = "Start X ({})".format(unit_label) if unit_label else "Start X"
        self.colCustomStartX.Header = coord_header_x

        if unit_label:
            self.lblSpokeLength.Text = "Spoke length ({})".format(unit_label)
            self.lblRingStart.Text = "Inner ring radius ({})".format(unit_label)
        self.txtSpokeLength.Text = fmt_length(DEFAULT_SPOKE_LENGTH_FT)

        # --- Orthogonal ---
        self.dgOrthoX.ItemsSource = ObservableCollection[object]()
        self.dgOrthoY.ItemsSource = ObservableCollection[object]()
        self._add_spacing_row(self.dgOrthoX, next_numeric_name, "0")
        self._add_spacing_row(self.dgOrthoY, next_alpha_name, "0")

        # --- Radial ---
        self.dgSpokes.ItemsSource = ObservableCollection[object]()
        self.dgRings.ItemsSource = ObservableCollection[object]()
        self._add_spacing_row(self.dgSpokes, next_alpha_name, "0", is_angle=True)
        self._add_spacing_row(self.dgRings, next_numeric_name, fmt_length(DEFAULT_RING_SPACING_FT))

        # --- Custom ---
        self.dgCustom.ItemsSource = ObservableCollection[object]()
        self._add_custom_row()

        # --- Auto from CAD ---
        self.dgCadPreview.ItemsSource = ObservableCollection[object]()
        self._load_cad_imports()

        self.btnAddOrthoX.Click += lambda s, e: self._add_spacing_row(self.dgOrthoX, next_numeric_name, fmt_length(DEFAULT_BAY_SPACING_FT))
        self.btnRemoveOrthoX.Click += lambda s, e: self._remove_row(self.dgOrthoX)
        self.btnAddOrthoY.Click += lambda s, e: self._add_spacing_row(self.dgOrthoY, next_alpha_name, fmt_length(DEFAULT_BAY_SPACING_FT))
        self.btnRemoveOrthoY.Click += lambda s, e: self._remove_row(self.dgOrthoY)

        self.btnAddSpoke.Click += lambda s, e: self._add_spacing_row(self.dgSpokes, next_alpha_name, str(DEFAULT_ANGLE_SPACING_DEG), is_angle=True)
        self.btnRemoveSpoke.Click += lambda s, e: self._remove_row(self.dgSpokes)
        self.btnAddRing.Click += lambda s, e: self._add_spacing_row(self.dgRings, next_numeric_name, fmt_length(DEFAULT_RING_SPACING_FT))
        self.btnRemoveRing.Click += lambda s, e: self._remove_row(self.dgRings)

        self.btnAddCustom.Click += lambda s, e: self._add_custom_row()
        self.btnRemoveCustom.Click += lambda s, e: self._remove_row(self.dgCustom)

        self.cmbCadImport.SelectionChanged += self.on_cad_import_changed
        self.btnScanLayer.Click += self.on_scan_layer

        self.btnCreate.Click += self.on_create
        self.btnClose.Click += self.on_close

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
    # Row helpers shared by the Orthogonal / Radial tables
    # ------------------------------------------------------------------

    def _add_spacing_row(self, grid, name_func, default_spacing_text, is_angle=False):
        coll = grid.ItemsSource
        row = SpacingRow(coll.Count + 1, name_func(coll.Count + 1), default_spacing_text)
        coll.Add(row)
        self.lblStatus.Text = "Ready."

    def _add_custom_row(self):
        coll = self.dgCustom.ItemsSource
        n = coll.Count + 1
        coll.Add(CustomRow(n, "Grid {}".format(n), "0", "0", fmt_length(DEFAULT_BAY_SPACING_FT), "0"))
        self.lblStatus.Text = "Ready."

    def _remove_row(self, grid):
        row = grid.SelectedItem
        coll = grid.ItemsSource
        if row is None:
            forms.alert("Select a row to remove.")
            return
        coll.Remove(row)
        for i, r in enumerate(coll):
            r.Index = i + 1
        self.lblStatus.Text = "Ready."

    def _read_spacing_rows(self, grid, is_angle=False):
        """Returns list of (name, cumulative_value) tuples, raises ValueError
        with a readable message on the first bad row."""
        coll = grid.ItemsSource
        result = []
        cumulative = 0.0
        for row in coll:
            name = (row.Name or "").strip()
            if not name:
                raise ValueError("Row {}: name is required.".format(row.Index))
            try:
                delta = float(str(row.Spacing).strip()) if is_angle else display_to_internal_length(row.Spacing)
            except Exception:
                raise ValueError("Row {}: invalid spacing '{}'.".format(row.Index, row.Spacing))
            cumulative += delta
            result.append((name, cumulative))
        return result

    # ------------------------------------------------------------------
    # Grid creation primitives
    # ------------------------------------------------------------------

    def _create_line_grid(self, name, p0, p1):
        line = Line.CreateBound(p0, p1)
        g = Grid.Create(doc, line)
        g.Name = name
        return g

    def _create_arc_grid(self, name, center, radius, start_angle_rad, end_angle_rad):
        arc = Arc.Create(center, radius, start_angle_rad, end_angle_rad, XYZ.BasisX, XYZ.BasisY)
        g = Grid.Create(doc, arc)
        g.Name = name
        return g

    # ------------------------------------------------------------------
    # Orthogonal
    # ------------------------------------------------------------------

    def _create_orthogonal(self):
        x_rows = self._read_spacing_rows(self.dgOrthoX)  # [(name, x_ft)]
        y_rows = self._read_spacing_rows(self.dgOrthoY)   # [(name, y_ft)]

        if not x_rows and not y_rows:
            raise ValueError("Add at least one X or Y grid row.")

        if y_rows:
            y_vals = [v for _, v in y_rows]
            y_min, y_max = min(y_vals) - EXTENT_MARGIN_FT, max(y_vals) + EXTENT_MARGIN_FT
        else:
            y_min, y_max = -FALLBACK_HALF_LEN_FT, FALLBACK_HALF_LEN_FT

        if x_rows:
            x_vals = [v for _, v in x_rows]
            x_min, x_max = min(x_vals) - EXTENT_MARGIN_FT, max(x_vals) + EXTENT_MARGIN_FT
        else:
            x_min, x_max = -FALLBACK_HALF_LEN_FT, FALLBACK_HALF_LEN_FT

        created, failed = [], []
        for name, x in x_rows:
            try:
                self._create_line_grid(name, XYZ(x, y_min, 0), XYZ(x, y_max, 0))
                created.append(name)
            except Exception as ex:
                failed.append("'{}': {}".format(name, ex))
        for name, y in y_rows:
            try:
                self._create_line_grid(name, XYZ(x_min, y, 0), XYZ(x_max, y, 0))
                created.append(name)
            except Exception as ex:
                failed.append("'{}': {}".format(name, ex))
        return created, failed

    # ------------------------------------------------------------------
    # Radial
    # ------------------------------------------------------------------

    def _create_radial(self):
        try:
            cx = display_to_internal_length(self.txtCenterX.Text)
            cy = display_to_internal_length(self.txtCenterY.Text)
        except Exception:
            raise ValueError("Invalid center X/Y.")
        try:
            start_angle = float(self.txtStartAngle.Text)
        except Exception:
            raise ValueError("Invalid start angle.")
        try:
            spoke_len = display_to_internal_length(self.txtSpokeLength.Text)
        except Exception:
            raise ValueError("Invalid spoke length.")
        try:
            inner_r = display_to_internal_length(self.txtRingStart.Text)
        except Exception:
            raise ValueError("Invalid inner ring radius.")

        spoke_rows = self._read_spacing_rows(self.dgSpokes, is_angle=True)  # [(name, cum_deg)]
        ring_rows = self._read_spacing_rows(self.dgRings)                  # [(name, cum_radius_ft)]

        if not spoke_rows and not ring_rows:
            raise ValueError("Add at least one spoke or ring row.")

        center = XYZ(cx, cy, 0)
        created, failed = [], []

        for name, cum_deg in spoke_rows:
            angle_rad = math.radians(start_angle + cum_deg)
            p0 = XYZ(cx + inner_r * math.cos(angle_rad), cy + inner_r * math.sin(angle_rad), 0)
            p1 = XYZ(cx + spoke_len * math.cos(angle_rad), cy + spoke_len * math.sin(angle_rad), 0)
            try:
                self._create_line_grid(name, p0, p1)
                created.append(name)
            except Exception as ex:
                failed.append("'{}': {}".format(name, ex))

        if ring_rows:
            if spoke_rows:
                total_sweep_deg = max(v for _, v in spoke_rows)
                if total_sweep_deg <= 0:
                    total_sweep_deg = 359.9
            else:
                total_sweep_deg = 359.9
            total_sweep_deg = min(total_sweep_deg, 359.9)
            start_rad = math.radians(start_angle)
            end_rad = math.radians(start_angle + total_sweep_deg)
            for name, radius in ring_rows:
                if radius <= 0:
                    failed.append("'{}': ring radius must be greater than zero.".format(name))
                    continue
                try:
                    self._create_arc_grid(name, center, radius, start_rad, end_rad)
                    created.append(name)
                except Exception as ex:
                    failed.append("'{}': {}".format(name, ex))

        return created, failed

    # ------------------------------------------------------------------
    # Custom
    # ------------------------------------------------------------------

    def _create_custom(self):
        coll = self.dgCustom.ItemsSource
        if coll is None or coll.Count == 0:
            raise ValueError("Add at least one custom grid row.")

        created, failed = [], []
        for row in coll:
            name = (row.Name or "").strip()
            if not name:
                failed.append("Row {}: name is required.".format(row.Index))
                continue
            try:
                sx = display_to_internal_length(row.StartX)
                sy = display_to_internal_length(row.StartY)
                ex_ = display_to_internal_length(row.EndX)
                ey = display_to_internal_length(row.EndY)
            except Exception:
                failed.append("Row {} ('{}'): invalid coordinates.".format(row.Index, name))
                continue
            try:
                self._create_line_grid(name, XYZ(sx, sy, 0), XYZ(ex_, ey, 0))
                created.append(name)
            except Exception as ex:
                failed.append("'{}': {}".format(name, ex))
        return created, failed

    # ------------------------------------------------------------------
    # Auto from CAD
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
            self.lblCadInfo.Text = "No imported or linked CAD drawings were found in this model."

    def on_cad_import_changed(self, sender, e):
        item = self.cmbCadImport.SelectedItem
        self.cmbCadLayer.ItemsSource = None
        if item is None:
            return
        imp = item.Value
        cat = imp.Category
        layers = []
        if cat is not None:
            try:
                for sub in cat.SubCategories:
                    layers.append(ComboItem(sub.Name, sub))
            except Exception:
                pass
        layers.sort(key=lambda i: i.Display)
        self.cmbCadLayer.ItemsSource = layers
        if layers:
            self.cmbCadLayer.SelectedIndex = 0

    def on_scan_layer(self, sender, e):
        cad_item = self.cmbCadImport.SelectedItem
        layer_item = self.cmbCadLayer.SelectedItem
        if cad_item is None or layer_item is None:
            forms.alert("Pick a CAD import and a layer first.")
            return

        import_instance = cad_item.Value
        layer_category = layer_item.Value

        try:
            opts = Options()
            opts.IncludeNonVisibleObjects = True
            geom = import_instance.get_Geometry(opts)
        except Exception as ex:
            forms.alert("Could not read geometry from this CAD import:\n{}".format(ex))
            return

        raw_lines = []
        raw_texts = []
        DBText = self._try_get_text_class()
        self._walk_geometry(geom, layer_category.Id, raw_lines, raw_texts, DBText)

        if not raw_lines:
            self.dgCadPreview.ItemsSource = ObservableCollection[object]()
            self.lblCadInfo.Text = (
                "No straight lines were found on layer '{}'. Curves/arcs and "
                "text-only layers are not scanned in this version.".format(layer_item.Display))
            self.lblStatus.Text = "Scan found 0 lines."
            return

        merged = self._cluster_lines(raw_lines)
        rows = self._build_preview_rows(merged, raw_texts)

        coll = ObservableCollection[object]()
        matched = 0
        for r in rows:
            coll.Add(r)
            if r.Source == "CAD text":
                matched += 1
        self.dgCadPreview.ItemsSource = coll

        self.lblCadInfo.Text = (
            "Detected {} grid line(s) on layer '{}' ({} merged from {} raw segments). "
            "{} name(s) were read from CAD text; the rest were auto-numbered — "
            "please review before creating.".format(
                len(rows), layer_item.Display, len(merged), len(raw_lines), matched))
        self.lblStatus.Text = "Scan complete: {} candidate line(s).".format(len(rows))

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

    def _walk_geometry(self, geom_element, target_cat_id, lines, texts, DBText):
        if geom_element is None:
            return
        for obj in geom_element:
            if isinstance(obj, GeometryInstance):
                try:
                    inst_geom = obj.GetInstanceGeometry()
                except Exception:
                    inst_geom = None
                self._walk_geometry(inst_geom, target_cat_id, lines, texts, DBText)
                continue

            style_id = None
            try:
                style_id = obj.GraphicsStyleId
            except Exception:
                style_id = None
            if style_id is None or style_id == ElementId.InvalidElementId:
                continue
            gs = doc.GetElement(style_id)
            if gs is None or gs.GraphicsStyleCategory is None:
                continue
            if gs.GraphicsStyleCategory.Id != target_cat_id:
                continue

            if isinstance(obj, Line):
                lines.append((obj.GetEndPoint(0), obj.GetEndPoint(1)))
            elif DBText is not None and isinstance(obj, DBText):
                try:
                    texts.append((obj.Position, obj.Value))
                except Exception:
                    pass

    def _cluster_lines(self, raw_lines, angle_tol_deg=1.0, offset_tol_ft=0.5):
        clusters = []
        for p0, p1 in raw_lines:
            dx, dy = p1.X - p0.X, p1.Y - p0.Y
            length = math.sqrt(dx * dx + dy * dy)
            if length < 1e-6:
                continue
            ux, uy = dx / length, dy / length
            angle = math.degrees(math.atan2(uy, ux)) % 180.0
            nx, ny = -uy, ux
            offset = p0.X * nx + p0.Y * ny
            placed = False
            for c in clusters:
                da = abs(c['angle'] - angle)
                da = min(da, 180.0 - da)
                if da <= angle_tol_deg and abs(c['offset'] - offset) <= offset_tol_ft:
                    c['points'].append(p0)
                    c['points'].append(p1)
                    placed = True
                    break
            if not placed:
                clusters.append({'angle': angle, 'offset': offset, 'dir': (ux, uy), 'points': [p0, p1]})

        merged = []
        for c in clusters:
            ux, uy = c['dir']
            pts = c['points']
            projs = sorted(pts, key=lambda p: p.X * ux + p.Y * uy)
            merged.append((projs[0], projs[-1]))
        return merged

    def _build_preview_rows(self, merged_lines, raw_texts, text_tol_ft=2.0):
        # sort roughly: mostly-vertical lines first (numbered), then
        # mostly-horizontal (lettered), then diagonal (generic).
        verticals, horizontals, others = [], [], []
        for p0, p1 in merged_lines:
            dx, dy = p1.X - p0.X, p1.Y - p0.Y
            if abs(dx) < 1e-6 and abs(dy) < 1e-6:
                continue
            angle = abs(math.degrees(math.atan2(dy, dx))) % 180.0
            if angle > 90:
                angle = 180 - angle
            if angle >= 60:
                verticals.append((p0, p1))
            elif angle <= 30:
                horizontals.append((p0, p1))
            else:
                others.append((p0, p1))

        verticals.sort(key=lambda seg: (seg[0].X + seg[1].X) / 2.0)
        horizontals.sort(key=lambda seg: (seg[0].Y + seg[1].Y) / 2.0)

        rows = []

        def make_row(p0, p1, default_name):
            name, source = default_name, "auto-numbered"
            match = self._find_nearby_text(p0, p1, raw_texts, text_tol_ft)
            if match:
                name, source = match, "CAD text"
            return CadPreviewRow(name, p0, p1, source)

        for i, (p0, p1) in enumerate(verticals):
            rows.append(make_row(p0, p1, next_numeric_name(i + 1)))
        for i, (p0, p1) in enumerate(horizontals):
            rows.append(make_row(p0, p1, next_alpha_name(i + 1)))
        for i, (p0, p1) in enumerate(others):
            rows.append(make_row(p0, p1, "G{}".format(i + 1)))

        return rows

    def _find_nearby_text(self, p0, p1, raw_texts, tol_ft):
        best_name, best_dist = None, tol_ft
        for pos, value in raw_texts:
            if not value:
                continue
            for pt in (p0, p1):
                dist = math.sqrt((pos.X - pt.X) ** 2 + (pos.Y - pt.Y) ** 2)
                if dist < best_dist:
                    best_dist = dist
                    best_name = value.strip()
        return best_name

    def _create_from_cad_preview(self):
        coll = self.dgCadPreview.ItemsSource
        if coll is None or coll.Count == 0:
            raise ValueError("Scan a CAD layer first.")

        created, failed = [], []
        for row in coll:
            if not row.Include:
                continue
            name = (row.Name or "").strip()
            if not name:
                failed.append("A row with blank name was skipped.")
                continue
            try:
                self._create_line_grid(name, row._start_ft, row._end_ft)
                created.append(name)
            except Exception as ex:
                failed.append("'{}': {}".format(name, ex))
        return created, failed

    # ------------------------------------------------------------------
    # Create (dispatches based on the active tab)
    # ------------------------------------------------------------------

    def on_create(self, sender, e):
        tab_index = self.tabMain.SelectedIndex
        handler = {
            0: self._create_orthogonal,
            1: self._create_radial,
            2: self._create_custom,
            3: self._create_from_cad_preview,
        }.get(tab_index)

        if handler is None:
            return

        t = Transaction(doc, "Define Grid: Create Grids")
        t.Start()
        try:
            created, failed = handler()
            t.Commit()
        except ValueError as ex:
            t.RollBack()
            forms.alert(str(ex), title="Define Grid")
            return
        except Exception as ex:
            t.RollBack()
            logger.error("Define Grid failed: {}".format(ex))
            forms.alert("Failed to create grids:\n{}".format(ex), title="Define Grid")
            self.lblStatus.Text = "Failed."
            return

        if failed:
            forms.alert(
                "Created {} grid(s).\n\nSome rows could not be created:\n{}".format(
                    len(created), "\n".join(failed)),
                title="Define Grid")
            self.lblStatus.Text = "Created {} grid(s), {} failed.".format(len(created), len(failed))
        else:
            self.lblStatus.Text = "Created {} grid(s).".format(len(created))

    def on_close(self, sender, e):
        self.Close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if doc is None:
    forms.alert("No active Revit document.", exitscript=True)

try:
    xaml_file = script.get_bundle_file('ui.xaml')
    window = DefineGridWindow(xaml_file)
    window.ShowDialog()
except Exception as ex:
    import traceback
    _safe_log("Entry point failed: {}\n{}".format(ex, traceback.format_exc()))
