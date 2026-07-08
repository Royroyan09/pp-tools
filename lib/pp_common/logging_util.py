# -*- coding: utf-8 -*-
"""Crash-safe file logging shared by every auto-modelling tool.

Writes straight to a plain text file instead of pyRevit's logger/output
console: on some engine threads, the console's lazy initialization
requires the UI thread and raising through it can bring down the whole
Revit process. Plain file I/O has no such risk.
"""


def safe_log(log_path, msg):
    try:
        with open(log_path, 'a') as f:
            f.write(msg + "\n")
    except Exception:
        pass
