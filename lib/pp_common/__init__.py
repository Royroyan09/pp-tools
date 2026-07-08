# -*- coding: utf-8 -*-
"""Shared library for PP Tools' "auto-modelling" tools (Structure panel
and beyond): CAD reading, unit conversion, shape geometry, label
matching and placement helpers used by more than one pushbutton.

Each pushbutton's script.py adds this package's parent folder (the
extension's "lib" directory) to sys.path itself before importing, since
pyRevit's own auto-added lib path cannot be relied on for a bootstrap
that also has to work when a module is run standalone for testing.
"""
