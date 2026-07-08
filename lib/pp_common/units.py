# -*- coding: utf-8 -*-
"""Unit conversion and misc IronPython-safe helpers shared by every
auto-modelling tool. All lengths are shown/edited in the project's
display units; doc is passed explicitly rather than read from a
module-level global so this module has no dependency on any one
tool's script.
"""
import clr
clr.AddReference('RevitAPI')

from Autodesk.Revit.DB import Element, SpecTypeId, UnitUtils


def get_name(element):
    """IronPython-safe Element.Name accessor."""
    if element is None:
        return ""
    try:
        return Element.Name.__get__(element)
    except Exception:
        try:
            return element.Name
        except Exception:
            return "<unnamed>"


def get_length_unit_type_id(doc):
    try:
        return doc.GetUnits().GetFormatOptions(SpecTypeId.Length).GetUnitTypeId()
    except Exception:
        return None


def internal_to_display_length(doc, value_ft):
    unit_id = get_length_unit_type_id(doc)
    try:
        return UnitUtils.ConvertFromInternalUnits(value_ft, unit_id)
    except Exception:
        return value_ft


def display_to_internal_length(doc, text):
    unit_id = get_length_unit_type_id(doc)
    value = float(str(text).strip().replace(",", "."))
    try:
        return UnitUtils.ConvertToInternalUnits(value, unit_id)
    except Exception:
        return value


def length_unit_label(doc):
    unit_id = get_length_unit_type_id(doc)
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


def fmt_length(doc, value_ft):
    return fmt_num(internal_to_display_length(doc, value_ft))
