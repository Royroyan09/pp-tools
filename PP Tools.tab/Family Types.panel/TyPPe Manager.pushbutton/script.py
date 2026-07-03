# -*- coding: utf-8 -*-
"""TyPPe Manager

Browse family types (system families such as Walls / Floors / Roofs /
Ceilings, and loadable families) and edit compound structure layers
(thickness, material, function) and type parameters from a single dialog.
"""
from __future__ import print_function

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import System
from System import Action
from System.IO import FileStream, FileMode, FileAccess
from System.Windows import Visibility, MessageBox, MessageBoxButton, MessageBoxImage, MessageBoxResult
from System.Windows.Controls import TreeViewItem
from System.Windows.Threading import DispatcherPriority
from System.Windows.Media.Imaging import BitmapImage, BitmapCacheOption
from System.Collections.Generic import List as NetList
from System.Collections.ObjectModel import ObservableCollection

from Autodesk.Revit.DB import (
    FilteredElementCollector, Element, Transaction, Material,
    MaterialFunctionAssignment, CompoundStructureLayer, StorageType,
    ElementId, CategoryType, SpecTypeId, UnitUtils
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


FUNCTION_NAMES = [
    "Structure", "Substrate", "Insulation", "Finish1", "Finish2",
    "Membrane", "StructuralDeck",
]

FUNCTION_MAP = {
    "Structure": MaterialFunctionAssignment.Structure,
    "Substrate": MaterialFunctionAssignment.Substrate,
    "Insulation": MaterialFunctionAssignment.Insulation,
    "Finish1": MaterialFunctionAssignment.Finish1,
    "Finish2": MaterialFunctionAssignment.Finish2,
    "Membrane": MaterialFunctionAssignment.Membrane,
    "StructuralDeck": MaterialFunctionAssignment.StructuralDeck,
}

NO_MATERIAL_LABEL = "<By Category>"


# ---------------------------------------------------------------------------
# IronPython-safe Element.Name accessors
# ---------------------------------------------------------------------------
# Element.Name is ambiguous when accessed directly from IronPython on some
# Revit API classes (raises AttributeError); the CLR property descriptor
# must be invoked explicitly instead.

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


def set_name(element, new_name):
    try:
        Element.Name.__set__(element, new_name)
    except Exception:
        element.Name = new_name


# ---------------------------------------------------------------------------
# Unit helpers (length is always shown/edited in the project's display units)
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
# Row / item view models bound to WPF grids and the category combo
# ---------------------------------------------------------------------------

class ComboItem(object):
    def __init__(self, display, value):
        self.Display = display
        self.Value = value


class LayerRow(object):
    def __init__(self, index, function, material_name, thickness_text, is_core):
        self.Index = index
        self.Function = function
        self.MaterialName = material_name
        self.Thickness = thickness_text
        self.IsCore = is_core


class ParamRow(object):
    def __init__(self, name, value, param_id, storage_type, is_readonly):
        self.Name = name
        self.Value = value
        self.ParamId = param_id
        self.StorageType = storage_type
        self.IsReadOnly = is_readonly


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class TypeManagerWindow(forms.WPFWindow):

    def __init__(self, xaml_file):
        forms.WPFWindow.__init__(self, xaml_file)

        self.lblDocTitle.Text = doc.Title or "Untitled"
        self._load_logo()

        self._materials = self._load_materials()
        self._all_types = []
        self._current_type = None
        self._current_cs = None
        self._dirty = False

        self.colFunction.ItemsSource = list(FUNCTION_NAMES)
        self.colMaterial.ItemsSource = [NO_MATERIAL_LABEL] + [m[0] for m in self._materials]

        self._load_categories()
        self._rebuild_tree()
        self._set_detail_enabled(False)

        self.cmbCategory.SelectionChanged += self.on_filters_changed
        self.txtSearch.TextChanged += self.on_filters_changed
        self.treeTypes.SelectedItemChanged += self.on_tree_selection_changed
        self.btnRefresh.Click += self.on_refresh
        self.btnNewType.Click += self.on_new_type
        self.btnDuplicate.Click += self.on_duplicate
        self.btnRename.Click += self.on_rename
        self.btnDelete.Click += self.on_delete
        self.btnAddLayer.Click += self.on_add_layer
        self.btnRemoveLayer.Click += self.on_remove_layer
        self.dgLayers.CellEditEnding += self.on_layers_cell_edit
        self.dgParameters.CellEditEnding += self.on_params_cell_edit
        self.btnSave.Click += self.on_save
        self.btnCancel.Click += self.on_cancel

    # ------------------------------------------------------------------
    # Data loading
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

    def _load_materials(self):
        mats = FilteredElementCollector(doc).OfClass(Material).ToElements()
        items = [(get_name(m), m.Id) for m in mats]
        items.sort(key=lambda x: x[0].lower())
        return items

    def _collect_all_types(self):
        all_element_types = FilteredElementCollector(doc).WhereElementIsElementType().ToElements()
        result = []
        for et in all_element_types:
            cat = et.Category
            if cat is None:
                continue
            if cat.CategoryType not in (CategoryType.Model, CategoryType.Annotation):
                continue
            try:
                fam_name = et.FamilyName
            except Exception:
                fam_name = None
            if not fam_name:
                continue
            result.append(et)
        return result

    def _load_categories(self):
        self._all_types = self._collect_all_types()
        cat_names = sorted(set(t.Category.Name for t in self._all_types))
        items = [ComboItem("All Categories ({})".format(len(self._all_types)), None)]
        for name in cat_names:
            count = sum(1 for t in self._all_types if t.Category.Name == name)
            items.append(ComboItem("{} ({})".format(name, count), name))
        self.cmbCategory.ItemsSource = items
        self.cmbCategory.SelectedIndex = 0

    # ------------------------------------------------------------------
    # Tree building
    # ------------------------------------------------------------------

    def on_filters_changed(self, sender, e):
        self._rebuild_tree()

    def _rebuild_tree(self):
        cat_item = self.cmbCategory.SelectedItem
        cat_filter = cat_item.Value if cat_item else None
        search = (self.txtSearch.Text or "").strip().lower()

        filtered = []
        for t in self._all_types:
            if cat_filter and t.Category.Name != cat_filter:
                continue
            tname = get_name(t)
            fam = t.FamilyName
            if search and search not in tname.lower() and search not in fam.lower():
                continue
            filtered.append(t)

        tree_data = {}
        for t in filtered:
            cat_name = t.Category.Name
            fam = t.FamilyName
            tree_data.setdefault(cat_name, {}).setdefault(fam, []).append(t)

        self.treeTypes.Items.Clear()
        auto_expand = bool(search) or len(tree_data) <= 1
        for cat_name in sorted(tree_data.keys()):
            cat_node = TreeViewItem()
            cat_node.Header = cat_name
            cat_node.IsExpanded = auto_expand
            families = tree_data[cat_name]
            for fam_name in sorted(families.keys()):
                fam_node = TreeViewItem()
                fam_node.Header = "{} ({})".format(fam_name, len(families[fam_name]))
                fam_node.IsExpanded = auto_expand or bool(search)
                type_list = sorted(families[fam_name], key=lambda x: get_name(x))
                for t in type_list:
                    type_node = TreeViewItem()
                    type_node.Header = get_name(t)
                    type_node.Tag = t
                    fam_node.Items.Add(type_node)
                cat_node.Items.Add(fam_node)
            self.treeTypes.Items.Add(cat_node)

        self.lblStatus.Text = "{} type(s) found.".format(len(filtered))

    def _select_type_in_tree(self, elem_type):
        def find(items):
            for item in items:
                tag = getattr(item, 'Tag', None)
                if tag is not None and tag.Id == elem_type.Id:
                    return item
                found = find(item.Items)
                if found is not None:
                    item.IsExpanded = True
                    return found
            return None

        node = find(self.treeTypes.Items)
        if node is not None:
            node.IsSelected = True
            node.BringIntoView()

    # ------------------------------------------------------------------
    # Selection -> detail panel
    # ------------------------------------------------------------------

    def on_tree_selection_changed(self, sender, e):
        node = self.treeTypes.SelectedItem
        elem_type = getattr(node, 'Tag', None) if node is not None else None
        if elem_type is None:
            return

        if self._dirty:
            result = MessageBox.Show(
                "You have unsaved changes for the current type. Discard them?",
                "Unsaved changes", MessageBoxButton.YesNo, MessageBoxImage.Warning)
            if result != MessageBoxResult.Yes:
                self._select_type_in_tree(self._current_type)
                return

        self._load_type(elem_type)

    def _load_type(self, elem_type):
        self._current_type = elem_type
        self._dirty = False

        name = get_name(elem_type)
        fam = getattr(elem_type, 'FamilyName', '')
        cat = elem_type.Category.Name if elem_type.Category else ''
        self.lblSelectedType.Text = name
        self.lblSelectedSub.Text = "{}   |   Family: {}".format(cat, fam)

        try:
            cs = elem_type.GetCompoundStructure()
        except Exception:
            cs = None
        self._current_cs = cs

        if cs is not None:
            self.tabStructureItem.Visibility = Visibility.Visible
            self.btnAddLayer.IsEnabled = True
            self.btnRemoveLayer.IsEnabled = True
            self._load_layers(cs)
        else:
            self.tabStructureItem.Visibility = Visibility.Collapsed
            self.btnAddLayer.IsEnabled = False
            self.btnRemoveLayer.IsEnabled = False
            self.dgLayers.ItemsSource = None
            if self.tabDetails.SelectedItem == self.tabStructureItem:
                self.tabDetails.SelectedIndex = 1

        self._load_parameters(elem_type)
        self._set_detail_enabled(True)
        self.lblStatus.Text = "Loaded '{}'.".format(name)

    def _load_layers(self, cs):
        layers = cs.GetLayers()
        first_core = cs.GetFirstCoreLayerIndex()
        last_core = cs.GetLastCoreLayerIndex()

        coll = ObservableCollection[object]()
        total = 0.0
        for i in range(layers.Count):
            layer = layers[i]
            width_ft = layer.Width
            total += width_ft
            mat = doc.GetElement(layer.MaterialId)
            mat_name = get_name(mat) if mat else NO_MATERIAL_LABEL
            func_name = layer.Function.ToString()
            is_core = first_core <= i <= last_core
            coll.Add(LayerRow(i + 1, func_name, mat_name, fmt_length(width_ft), is_core))

        self.dgLayers.ItemsSource = coll
        unit_label = length_unit_label()
        self.colThickness.Header = "Thickness ({})".format(unit_label) if unit_label else "Thickness"
        self.lblTotalThickness.Text = "Total thickness: {} {}".format(fmt_length(total), unit_label)

    def _load_parameters(self, elem_type):
        rows = []
        seen = set()
        for p in elem_type.Parameters:
            try:
                pname = p.Definition.Name
            except Exception:
                continue
            if pname in seen or p.IsReadOnly:
                continue
            seen.add(pname)

            val_str = None
            try:
                val_str = p.AsValueString()
            except Exception:
                val_str = None
            if val_str is None:
                try:
                    if p.StorageType == StorageType.String:
                        val_str = p.AsString() or ""
                    elif p.StorageType == StorageType.Integer:
                        val_str = str(p.AsInteger())
                    elif p.StorageType == StorageType.Double:
                        val_str = str(p.AsDouble())
                    elif p.StorageType == StorageType.ElementId:
                        eid = p.AsElementId()
                        el = doc.GetElement(eid) if eid and eid != ElementId.InvalidElementId else None
                        val_str = get_name(el) if el else ""
                    else:
                        val_str = ""
                except Exception:
                    val_str = ""

            rows.append(ParamRow(pname, val_str, p.Id, p.StorageType, p.IsReadOnly))

        rows.sort(key=lambda r: r.Name)
        self.dgParameters.ItemsSource = rows

    # ------------------------------------------------------------------
    # Layer editing
    # ------------------------------------------------------------------

    def on_add_layer(self, sender, e):
        if self._current_cs is None:
            forms.alert("This type does not use a compound structure.")
            return
        coll = self.dgLayers.ItemsSource
        default_mat = self._materials[0][0] if self._materials else NO_MATERIAL_LABEL
        coll.Add(LayerRow(coll.Count + 1, "Substrate", default_mat, "0", False))
        self._mark_dirty()
        self._recompute_total()

    def on_remove_layer(self, sender, e):
        row = self.dgLayers.SelectedItem
        coll = self.dgLayers.ItemsSource
        if row is None or coll is None:
            forms.alert("Select a layer row to remove.")
            return
        if coll.Count <= 1:
            forms.alert("A compound structure must keep at least one layer.")
            return
        coll.Remove(row)
        for i, r in enumerate(coll):
            r.Index = i + 1
        self._mark_dirty()
        self._recompute_total()

    def on_layers_cell_edit(self, sender, e):
        self._mark_dirty()
        # Background priority so this runs after the DataGrid finishes
        # committing the edited value into the bound LayerRow.
        self.Dispatcher.BeginInvoke(DispatcherPriority.Background, Action(self._recompute_total))

    def on_params_cell_edit(self, sender, e):
        self._mark_dirty()

    def _recompute_total(self):
        coll = self.dgLayers.ItemsSource
        if coll is None:
            return
        total_display = 0.0
        for row in coll:
            try:
                total_display += float(str(row.Thickness).strip().replace(",", "."))
            except Exception:
                pass
        self.lblTotalThickness.Text = "Total thickness: {} {}".format(
            fmt_num(total_display), length_unit_label())

    def _mark_dirty(self):
        self._dirty = True
        self.lblStatus.Text = "Unsaved changes."

    # ------------------------------------------------------------------
    # Type management actions
    # ------------------------------------------------------------------

    def on_new_type(self, sender, e):
        if self._current_type is None:
            forms.alert("Select an existing type first, then click 'New Type' "
                         "to create a new type based on it (Revit creates new "
                         "types by duplicating an existing one).")
            return
        self.on_duplicate(sender, e)

    def on_duplicate(self, sender, e):
        if self._current_type is None:
            forms.alert("Select a type to duplicate.")
            return
        base_name = get_name(self._current_type)
        new_name = forms.ask_for_string(
            default=base_name + " Copy", prompt="New type name:", title="Duplicate Type")
        if not new_name:
            return

        t = Transaction(doc, "TyPPe Manager: Duplicate Type")
        t.Start()
        try:
            new_type = self._current_type.Duplicate(new_name)
            t.Commit()
        except Exception as ex:
            t.RollBack()
            forms.alert("Duplicate failed:\n{}".format(ex))
            return

        self._load_categories()
        self._rebuild_tree()
        self._select_type_in_tree(new_type)
        self.lblStatus.Text = "Duplicated as '{}'.".format(new_name)

    def on_rename(self, sender, e):
        if self._current_type is None:
            forms.alert("Select a type to rename.")
            return
        old_name = get_name(self._current_type)
        new_name = forms.ask_for_string(default=old_name, prompt="New name:", title="Rename Type")
        if not new_name or new_name == old_name:
            return

        t = Transaction(doc, "TyPPe Manager: Rename Type")
        t.Start()
        try:
            set_name(self._current_type, new_name)
            t.Commit()
        except Exception as ex:
            t.RollBack()
            forms.alert("Rename failed:\n{}".format(ex))
            return

        self._rebuild_tree()
        self._select_type_in_tree(self._current_type)
        self.lblSelectedType.Text = new_name
        self.lblStatus.Text = "Renamed to '{}'.".format(new_name)

    def on_delete(self, sender, e):
        if self._current_type is None:
            forms.alert("Select a type to delete.")
            return
        name = get_name(self._current_type)
        result = MessageBox.Show(
            "Delete type '{}'?\nThis cannot be undone from this dialog.".format(name),
            "Confirm Delete", MessageBoxButton.YesNo, MessageBoxImage.Warning)
        if result != MessageBoxResult.Yes:
            return

        t = Transaction(doc, "TyPPe Manager: Delete Type")
        t.Start()
        try:
            doc.Delete(self._current_type.Id)
            t.Commit()
        except Exception as ex:
            t.RollBack()
            forms.alert("Delete failed (it may still be in use in the model):\n{}".format(ex))
            return

        self._current_type = None
        self._set_detail_enabled(False)
        self._load_categories()
        self._rebuild_tree()
        self.lblStatus.Text = "Deleted '{}'.".format(name)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def on_save(self, sender, e):
        if self._current_type is None:
            return
        elem_type = self._current_type

        t = Transaction(doc, "TyPPe Manager: Update '{}'".format(get_name(elem_type)))
        t.Start()
        try:
            if self._current_cs is not None and self.dgLayers.ItemsSource is not None:
                self._apply_layers(elem_type)
            self._apply_parameters(elem_type)
            t.Commit()
        except Exception as ex:
            t.RollBack()
            logger.error("TyPPe Manager save failed: {}".format(ex))
            forms.alert("Failed to save changes:\n{}".format(ex), title="TyPPe Manager")
            self.lblStatus.Text = "Save failed."
            return

        self._dirty = False
        self.lblStatus.Text = "Saved changes to '{}'.".format(get_name(elem_type))
        self._load_type(elem_type)

    def _apply_layers(self, elem_type):
        coll = self.dgLayers.ItemsSource
        mat_lookup = dict((n, i) for n, i in self._materials)

        net_layers = NetList[CompoundStructureLayer]()
        for row in coll:
            width_ft = display_to_internal_length(row.Thickness)
            if width_ft <= 0:
                raise ValueError("Layer #{} thickness must be greater than zero.".format(row.Index))
            func = FUNCTION_MAP.get(row.Function, MaterialFunctionAssignment.Structure)
            mat_id = mat_lookup.get(row.MaterialName, ElementId.InvalidElementId)
            net_layers.Add(CompoundStructureLayer(width_ft, func, mat_id))

        cs = self._current_cs
        cs.SetLayers(net_layers)
        elem_type.SetCompoundStructure(cs)

    def _apply_parameters(self, elem_type):
        rows = self.dgParameters.ItemsSource
        if rows is None:
            return
        for row in rows:
            if row.IsReadOnly:
                continue
            param = None
            for p in elem_type.Parameters:
                if p.Id == row.ParamId:
                    param = p
                    break
            if param is None or param.IsReadOnly:
                continue

            try:
                current_display = param.AsValueString()
            except Exception:
                current_display = None
            if current_display == row.Value:
                continue

            try:
                ok = param.SetValueString(row.Value)
                if not ok:
                    self._set_param_fallback(param, row.Value)
            except Exception:
                self._set_param_fallback(param, row.Value)

    def _set_param_fallback(self, param, text):
        try:
            if param.StorageType == StorageType.String:
                param.Set(text)
            elif param.StorageType == StorageType.Integer:
                param.Set(int(float(text)))
            elif param.StorageType == StorageType.Double:
                param.Set(display_to_internal_length(text))
        except Exception as ex:
            logger.debug("Could not set parameter '{}': {}".format(
                getattr(param.Definition, 'Name', '?'), ex))

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def on_refresh(self, sender, e):
        self._current_type = None
        self._set_detail_enabled(False)
        self._load_categories()
        self._rebuild_tree()
        self.lblStatus.Text = "Refreshed."

    def on_cancel(self, sender, e):
        if self._dirty:
            result = MessageBox.Show(
                "You have unsaved changes. Close anyway?",
                "Unsaved changes", MessageBoxButton.YesNo, MessageBoxImage.Warning)
            if result != MessageBoxResult.Yes:
                return
        self.Close()

    def _set_detail_enabled(self, flag):
        self.tabDetails.IsEnabled = flag
        self.btnDuplicate.IsEnabled = flag
        self.btnRename.IsEnabled = flag
        self.btnDelete.IsEnabled = flag
        self.btnSave.IsEnabled = flag
        if not flag:
            self.lblSelectedType.Text = "Select a type from the list"
            self.lblSelectedSub.Text = "No type selected"
            self.dgLayers.ItemsSource = None
            self.dgParameters.ItemsSource = None
            self.btnAddLayer.IsEnabled = False
            self.btnRemoveLayer.IsEnabled = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if doc is None:
    forms.alert("No active Revit document.", exitscript=True)

try:
    xaml_file = script.get_bundle_file('ui.xaml')
    window = TypeManagerWindow(xaml_file)
    window.ShowDialog()
except Exception as ex:
    import traceback
    _safe_log("Entry point failed: {}\n{}".format(ex, traceback.format_exc()))
