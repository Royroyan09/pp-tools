# -*- coding: utf-8 -*-
"""Capture View

Screen-captures the active view's viewport (not the whole Revit window),
optionally crops a strip off the right edge (e.g. to cut out the
ViewCube/navigation bar in 3D views), and saves it as a PNG via a save
dialog.
"""
import re

import clr
clr.AddReference("System.Drawing")

from System.Drawing import Bitmap, Graphics, Size, Rectangle
from System.Drawing.Imaging import ImageFormat
from System.Threading import Thread

from pyrevit import revit, forms, script

uidoc = revit.uidoc
active_view = uidoc.ActiveView
config = script.get_config()

uiview = None
for uv in uidoc.GetOpenUIViews():
    if uv.ViewId == active_view.Id:
        uiview = uv
        break

if not uiview:
    forms.alert("Could not find the active view's window.", exitscript=True)

rect = uiview.GetWindowRectangle()  # Autodesk.Revit.UI.Rectangle: Left/Top/Right/Bottom
width = rect.Right - rect.Left
height = rect.Bottom - rect.Top

CROP_PATTERN = re.compile(r'^\s*([0-9]*\.?[0-9]+)\s*(mm|cm|px)?\s*$', re.IGNORECASE)


def parse_crop(text, dpi_x):
    """Returns crop amount in pixels, or None if text is empty/invalid."""
    if not text or not text.strip():
        return 0
    match = CROP_PATTERN.match(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or 'px').lower()
    if unit == 'mm':
        return int(round(value / 25.4 * dpi_x))
    if unit == 'cm':
        return int(round(value / 2.54 * dpi_x))
    return int(round(value))


# resolve screen DPI (needed to convert mm/cm to pixels)
g_probe = Graphics.FromImage(Bitmap(1, 1))
dpi_x = g_probe.DpiX
g_probe.Dispose()

default_crop = config.get_option('right_crop', '0px')
crop_text = forms.ask_for_string(
    default=default_crop,
    prompt="Crop off the right edge (e.g. '50mm', '2cm', '150px'). "
           "Useful for cutting out the ViewCube/navigation bar. "
           "Leave as 0 for no crop.",
    title="Capture View")

if crop_text is None:
    script.exit()

crop_px = parse_crop(crop_text, dpi_x)
if crop_px is None:
    forms.alert("Could not understand '{}'. Use a number optionally "
               "followed by mm, cm, or px.".format(crop_text),
               exitscript=True)

crop_px = max(0, min(crop_px, width - 1))
config.right_crop = crop_text
script.save_config()

# let the ribbon popup/tooltip from the button click disappear and the
# view repaint before grabbing the screen; Join (unlike Sleep) pumps
# Windows messages while waiting, so the UI keeps redrawing
Thread.CurrentThread.Join(500)

# capture BEFORE showing any dialog so nothing can overlay the viewport
bmp = Bitmap(width, height)
g = Graphics.FromImage(bmp)
g.CopyFromScreen(rect.Left, rect.Top, 0, 0, Size(width, height))
g.Dispose()

if crop_px > 0:
    cropped = bmp.Clone(Rectangle(0, 0, width - crop_px, height), bmp.PixelFormat)
    bmp.Dispose()
    bmp = cropped

save_path = forms.save_file(file_ext='png',
                            default_name='{}_capture'.format(active_view.Name))
if save_path:
    bmp.Save(save_path, ImageFormat.Png)
    forms.alert("Saved to:\n{}".format(save_path))
bmp.Dispose()
