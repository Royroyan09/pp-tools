# -*- coding: utf-8 -*-
"""Small WPF-adjacent building blocks shared by every auto-modelling
tool's dialog. Kept free of any specific XAML/control names — each
tool's own window class still owns its bindings and layout."""


class ComboItem(object):
    """(Display, Value) pair for WPF ComboBox DisplayMemberPath binding."""

    def __init__(self, display, value):
        self.Display = display
        self.Value = value


class LayerSelection(object):
    """One logical 'layer' selection for scanning, backed by a LIST of
    real CAD Category objects -- a single Pick session (pp_common.
    cad_read.pick_points_on_cad_multi) can sweep up several actual DWG
    layers that all represent the same conceptual beam/label/
    perimeter/column layer, which happens on multi-block-flattened
    imports (confirmed against a real drawing with dozens of near-
    duplicate layer names). Every "layer" ComboBox item's Value is one
    of these (even a plain single-layer pick/dropdown choice just
    wraps a 1-element list) so every call site has one consistent
    shape to work with instead of sometimes-Category, sometimes-list."""

    def __init__(self, categories):
        self.categories = list(categories)

    @property
    def ids(self):
        return [c.Id for c in self.categories]

    @property
    def names(self):
        return [c.Name for c in self.categories]

    @property
    def display(self):
        names = self.names
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        return u"{} (+{} more)".format(names[0], len(names) - 1)


def set_layer_selection(combo, categories):
    """Sets a 'layer' ComboBox's current selection to a LayerSelection
    built from `categories`. Reuses one of the combo's existing per-
    layer items when the result is exactly one already-listed layer
    (the common case: a plain dropdown pick, or a Pick session that
    only touched one real layer); otherwise injects a synthetic
    combined item (Display shows 'FirstName (+N more)', flagged
    IsSynthetic so it's never mistaken for a real per-layer entry and
    is dropped/replaced rather than accumulating) at the top of the
    list and selects that. Shared by every 'layer' ComboBox across all
    four auto-modelling tools so a multi-layer pick result behaves
    identically everywhere -- see cad_read.pick_points_on_cad_multi/
    resolve_cad_layers for where `categories` usually comes from."""
    sel = LayerSelection(categories)
    base_items = [it for it in (combo.ItemsSource or [])
                  if not getattr(it, 'IsSynthetic', False)]
    if len(categories) == 1:
        for it in base_items:
            if it.Value.ids == sel.ids:
                combo.ItemsSource = base_items
                combo.SelectedItem = it
                return
    synthetic = ComboItem(sel.display, sel)
    synthetic.IsSynthetic = True
    combo.ItemsSource = [synthetic] + base_items
    combo.SelectedItem = synthetic


def select_layers_by_name(combo, names):
    """Restores a 'layer' ComboBox's selection from a list of
    persisted layer names (the counterpart to reading combo.
    SelectedItem.Value.names for capture_state) -- looks each name up
    among the combo's existing per-layer base items and re-applies the
    matching Categories as one LayerSelection via set_layer_selection.
    Names that no longer exist on this import (e.g. the CAD file
    changed) are silently dropped rather than raising -- the same
    tolerant behaviour the old single-layer _select_layer had."""
    if not names:
        return
    base_items = [it for it in (combo.ItemsSource or [])
                  if not getattr(it, 'IsSynthetic', False)]
    by_name = {}
    for it in base_items:
        for n in it.Value.names:
            by_name[n] = it.Value.categories[0]
    categories = [by_name[n] for n in names if n in by_name]
    if categories:
        set_layer_selection(combo, categories)
