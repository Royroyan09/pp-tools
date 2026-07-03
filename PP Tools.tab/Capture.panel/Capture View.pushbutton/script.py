# -*- coding: utf-8 -*-
"""Capture View

Screen-captures the active view's viewport (not the whole Revit window)
and saves it as a PNG file via a save dialog.
"""
import clr
clr.AddReference("System.Drawing")

from System.Drawing import Bitmap, Graphics, Size
from System.Drawing.Imaging import ImageFormat
from System.Threading import Thread

from pyrevit import revit, forms

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

# let the ribbon popup/tooltip from the button click disappear and the
# view repaint before grabbing the screen; Join (unlike Sleep) pumps
# Windows messages while waiting, so the UI keeps redrawing
Thread.CurrentThread.Join(500)

# capture BEFORE showing any dialog so nothing can overlay the viewport
bmp = Bitmap(width, height)
g = Graphics.FromImage(bmp)
g.CopyFromScreen(rect.Left, rect.Top, 0, 0, Size(width, height))
g.Dispose()

save_path = forms.save_file(file_ext='png',
                            default_name='{}_capture'.format(active_view.Name))
if save_path:
    bmp.Save(save_path, ImageFormat.Png)
    forms.alert("Saved to:\n{}".format(save_path))
bmp.Dispose()
