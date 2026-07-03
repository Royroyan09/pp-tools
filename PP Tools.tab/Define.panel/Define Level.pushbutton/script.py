# -*- coding: utf-8 -*-
"""Define Level

Create one or more Revit levels by typing a name and an elevation. Shows
existing levels for reference and can auto-create a floor plan view for
each new level.
"""
from __future__ import print_function

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
    UnitUtils, ViewFamilyType, ViewFamily, ViewPlan
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
# Unit helpers (elevation is always shown/edited in the project's display units)
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


DEFAULT_STORY_HEIGHT_FT = 12.0  # ~3.66 m; used only to prefill new rows


# ---------------------------------------------------------------------------
# Row view models bound to the WPF grids
# ---------------------------------------------------------------------------

class ExistingLevelRow(object):
    def __init__(self, name, elevation_text):
        self.Name = name
        self.Elevation = elevation_text


class NewLevelRow(object):
    def __init__(self, index, name, elevation_text):
        self.Index = index
        self.Name = name
        self.Elevation = elevation_text


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class DefineLevelWindow(forms.WPFWindow):

    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)

        self.lblDocTitle.Text = doc.Title or "Untitled"
        self._load_logo()

        unit_label = length_unit_label()
        header = "Elevation ({})".format(unit_label) if unit_label else "Elevation"
        self.colExistingElevation.Header = header
        self.colNewElevation.Header = header

        self._load_existing_levels()

        self.dgNewLevels.ItemsSource = ObservableCollection[object]()
        self._add_row()

        self.btnAddRow.Click += self.on_add_row
        self.btnRemoveRow.Click += self.on_remove_row
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

    def _load_existing_levels(self):
        levels = FilteredElementCollector(doc).OfClass(Level).ToElements()
        levels = sorted(levels, key=lambda lv: lv.Elevation)
        self._existing_levels = levels
        rows = [ExistingLevelRow(get_name(lv), fmt_length(lv.Elevation)) for lv in levels]
        self.dgExisting.ItemsSource = rows

    # ------------------------------------------------------------------
    # Row management
    # ------------------------------------------------------------------

    def _max_elevation_ft(self):
        best = None
        for lv in getattr(self, '_existing_levels', []):
            if best is None or lv.Elevation > best:
                best = lv.Elevation
        coll = self.dgNewLevels.ItemsSource
        if coll is not None:
            for row in coll:
                try:
                    ft = display_to_internal_length(row.Elevation)
                except Exception:
                    continue
                if best is None or ft > best:
                    best = ft
        return best if best is not None else 0.0

    def _next_default_name(self):
        n = len(getattr(self, '_existing_levels', []))
        coll = self.dgNewLevels.ItemsSource
        if coll is not None:
            n += coll.Count
        return "Level {}".format(n + 1)

    def _add_row(self):
        coll = self.dgNewLevels.ItemsSource
        next_elev_ft = self._max_elevation_ft() + DEFAULT_STORY_HEIGHT_FT
        row = NewLevelRow(coll.Count + 1, self._next_default_name(), fmt_length(next_elev_ft))
        coll.Add(row)

    def on_add_row(self, sender, e):
        self._add_row()
        self.lblStatus.Text = "Ready."

    def on_remove_row(self, sender, e):
        row = self.dgNewLevels.SelectedItem
        coll = self.dgNewLevels.ItemsSource
        if row is None:
            forms.alert("Select a row to remove.")
            return
        coll.Remove(row)
        for i, r in enumerate(coll):
            r.Index = i + 1
        self.lblStatus.Text = "Ready."

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def on_create(self, sender, e):
        coll = self.dgNewLevels.ItemsSource
        if coll is None or coll.Count == 0:
            forms.alert("Add at least one level row first.")
            return

        create_views = bool(self.chkCreateViews.IsChecked)
        floor_plan_vft = self._get_floor_plan_view_family_type() if create_views else None

        created = []
        failed = []

        t = Transaction(doc, "Define Level: Create Levels")
        t.Start()
        try:
            for row in coll:
                name = (row.Name or "").strip()
                if not name:
                    failed.append("Row {}: name is required.".format(row.Index))
                    continue
                try:
                    elev_ft = display_to_internal_length(row.Elevation)
                except Exception:
                    failed.append("Row {}: invalid elevation '{}'.".format(row.Index, row.Elevation))
                    continue

                try:
                    level = Level.Create(doc, elev_ft)
                    level.Name = name
                except Exception as ex:
                    failed.append("Row {} ('{}'): {}".format(row.Index, name, ex))
                    continue

                if create_views and floor_plan_vft is not None:
                    try:
                        ViewPlan.Create(doc, floor_plan_vft.Id, level.Id)
                    except Exception as ex:
                        _safe_log("Floor plan view creation failed for '{}': {}".format(name, ex))

                created.append(name)

            t.Commit()
        except Exception as ex:
            t.RollBack()
            logger.error("Define Level failed: {}".format(ex))
            forms.alert("Failed to create levels:\n{}".format(ex), title="Define Level")
            self.lblStatus.Text = "Failed."
            return

        self._load_existing_levels()
        self.dgNewLevels.ItemsSource = ObservableCollection[object]()
        self._add_row()

        if failed:
            forms.alert(
                "Created {} level(s).\n\nSome rows could not be created:\n{}".format(
                    len(created), "\n".join(failed)),
                title="Define Level")
            self.lblStatus.Text = "Created {} level(s), {} failed.".format(len(created), len(failed))
        else:
            self.lblStatus.Text = "Created {} level(s).".format(len(created))

    def _get_floor_plan_view_family_type(self):
        vfts = FilteredElementCollector(doc).OfClass(ViewFamilyType).ToElements()
        for vft in vfts:
            if vft.ViewFamily == ViewFamily.FloorPlan:
                return vft
        return None

    # ------------------------------------------------------------------

    def on_close(self, sender, e):
        self.Close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if doc is None:
    forms.alert("No active Revit document.", exitscript=True)

try:
    xaml_file = script.get_bundle_file('ui.xaml')
    window = DefineLevelWindow(xaml_file)
    window.ShowDialog()
except Exception as ex:
    import traceback
    _safe_log("Entry point failed: {}\n{}".format(ex, traceback.format_exc()))
