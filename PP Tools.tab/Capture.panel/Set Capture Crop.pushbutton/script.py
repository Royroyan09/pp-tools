# -*- coding: utf-8 -*-
"""Set Capture Crop

Sets how much to trim off the right edge of "Capture View" screenshots
(e.g. to cut out the ViewCube/navigation bar in 3D views). This only
changes the saved setting - it does not take a screenshot.
"""
from pyrevit import forms, script

from capture_crop import parse_crop, get_saved_crop_text, save_crop_text

crop_text = forms.ask_for_string(
    default=get_saved_crop_text(),
    prompt="Amount to crop off the right edge of Capture View "
           "screenshots (e.g. '50mm', '2cm', '150px'). Use 0 for no crop.",
    title="Set Capture Crop")

if crop_text is None:
    script.exit()

if parse_crop(crop_text) is None:
    forms.alert("Could not understand '{}'. Use a number optionally "
               "followed by mm, cm, or px.".format(crop_text),
               exitscript=True)

save_crop_text(crop_text)
forms.alert("Capture View will now crop {} off the right edge.".format(
    crop_text if crop_text.strip() else "nothing"))
