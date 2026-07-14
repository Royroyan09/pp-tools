# -*- coding: utf-8 -*-
"""Shared helpers for the Capture View / Set Capture Crop pushbuttons."""
import re

import clr
clr.AddReference("System.Drawing")

from System.Drawing import Bitmap, Graphics

from pyrevit import script

CONFIG_KEY = 'right_crop'
DEFAULT_CROP = '0px'

CROP_PATTERN = re.compile(r'^\s*([0-9]*\.?[0-9]+)\s*(mm|cm|px)?\s*$', re.IGNORECASE)


def get_screen_dpi_x():
    probe = Bitmap(1, 1)
    g = Graphics.FromImage(probe)
    dpi_x = g.DpiX
    g.Dispose()
    probe.Dispose()
    return dpi_x


def parse_crop(text, dpi_x=None):
    """Returns crop amount in pixels, or None if text is invalid."""
    if not text or not text.strip():
        return 0
    match = CROP_PATTERN.match(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or 'px').lower()
    if unit in ('mm', 'cm'):
        dpi_x = dpi_x if dpi_x is not None else get_screen_dpi_x()
        mm = value if unit == 'mm' else value * 10.0
        return int(round(mm / 25.4 * dpi_x))
    return int(round(value))


def get_saved_crop_text():
    config = script.get_config()
    return config.get_option(CONFIG_KEY, DEFAULT_CROP)


def save_crop_text(text):
    config = script.get_config()
    setattr(config, CONFIG_KEY, text)
    script.save_config()


def get_crop_pixels():
    """Crop amount in pixels from the saved setting. Falls back to 0
    (no crop) if the saved value is somehow invalid."""
    px = parse_crop(get_saved_crop_text())
    return px if px is not None else 0
