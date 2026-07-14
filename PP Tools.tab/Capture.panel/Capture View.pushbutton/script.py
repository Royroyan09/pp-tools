# -*- coding: utf-8 -*-
"""Capture View

Screen-captures the active view's viewport (not the whole Revit window),
optionally crops a strip off the right edge (e.g. to cut out the
ViewCube/navigation bar in 3D views) using the amount set via the
"Set Capture Crop" button, and saves it as a PNG via a save dialog.

Captures immediately with no dialog beforehand, so the active view's
window is grabbed exactly as it is at the moment of the click - nothing
is shown on screen that could get in the way of the capture.
"""
import clr
clr.AddReference("System.Drawing")

from System.Drawing import Bitmap, Graphics, Size, Rectangle
from System.Drawing.Imaging import ImageFormat
from System.Threading import Thread

from pyrevit import revit, forms, script

from capture_crop import get_crop_pixels

uidoc = revit.uidoc
active_view = uidoc.ActiveView

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

crop_px = max(0, min(get_crop_pixels(), width - 1))

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
