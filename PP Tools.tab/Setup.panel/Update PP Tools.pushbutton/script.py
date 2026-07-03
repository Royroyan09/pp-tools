# -*- coding: utf-8 -*-
"""Update PP Tools

Checks GitHub for a newer version of the PP Tools extension. If one is
available, downloads it, replaces the local files, and reloads pyRevit.
"""
import os
import shutil
import tempfile

import clr
clr.AddReference("System.IO.Compression.FileSystem")

from System.Net import WebClient, ServicePointManager, SecurityProtocolType
from System.IO.Compression import ZipFile

from pyrevit import forms

# ------------------------------------------------------------- settings --
# GitHub repository that hosts this extension, as "owner/repo-name".
GITHUB_REPO = "your-github-username/pp-tools"   # <-- EDIT THIS
BRANCH = "main"
# --------------------------------------------------------------------------

VERSION_URL = "https://raw.githubusercontent.com/{0}/{1}/VERSION".format(
    GITHUB_REPO, BRANCH)
ZIP_URL = "https://github.com/{0}/archive/refs/heads/{1}.zip".format(
    GITHUB_REPO, BRANCH)

# GitHub raw/zip downloads require TLS 1.2, which is not on by default
# in the .NET runtime pyRevit uses
ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12


def get_extension_root():
    """Walk up from this script to the *.extension folder."""
    path = os.path.dirname(os.path.abspath(__file__))
    while not path.lower().endswith(".extension"):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return path


def parse_version(text):
    try:
        return tuple(int(p) for p in text.strip().split("."))
    except (ValueError, AttributeError):
        return None


def read_local_version(ext_root):
    version_file = os.path.join(ext_root, "VERSION")
    if os.path.exists(version_file):
        with open(version_file, "r") as f:
            return f.read().strip()
    return None


def copy_tree(src, dst):
    """Copy src into dst, overwriting files; skips .git."""
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != ".git"]
        rel = os.path.relpath(root, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        if not os.path.isdir(target_dir):
            os.makedirs(target_dir)
        for name in files:
            shutil.copy2(os.path.join(root, name),
                         os.path.join(target_dir, name))


if "your-github-username" in GITHUB_REPO:
    forms.alert("The updater is not configured yet.\n\n"
                "Edit GITHUB_REPO at the top of:\n{0}".format(
                    os.path.abspath(__file__)),
                exitscript=True)

ext_root = get_extension_root()
if not ext_root:
    forms.alert("Could not locate the extension folder.", exitscript=True)

local_version = read_local_version(ext_root)

client = WebClient()
try:
    remote_version = client.DownloadString(VERSION_URL).strip()
except Exception as err:
    forms.alert("Could not check for updates.\n"
                "Check your internet connection and that the repository "
                "is reachable.\n\nDetails: {0}".format(err),
                exitscript=True)

if parse_version(remote_version) is None:
    forms.alert("The remote VERSION file is invalid: '{0}'".format(
        remote_version), exitscript=True)

if local_version and parse_version(remote_version) <= parse_version(local_version):
    forms.alert("PP Tools is up to date.\n\nInstalled version: {0}".format(
        local_version), exitscript=True)

if not forms.alert("A new version of PP Tools is available.\n\n"
                   "Installed: {0}\nAvailable: {1}\n\n"
                   "Download and install it now? pyRevit will reload "
                   "when done.".format(local_version or "unknown",
                                       remote_version),
                   yes=True, no=True):
    raise SystemExit

# download and extract fully BEFORE touching the installed files, so a
# failed download can never leave the extension half-updated
staging = tempfile.mkdtemp(prefix="pptools_update_")
zip_path = os.path.join(staging, "pptools.zip")
extract_dir = os.path.join(staging, "extracted")

try:
    client.DownloadFile(ZIP_URL, zip_path)
    ZipFile.ExtractToDirectory(zip_path, extract_dir)

    # the GitHub zip wraps everything in a single "<repo>-<branch>" folder
    entries = [os.path.join(extract_dir, e) for e in os.listdir(extract_dir)]
    roots = [e for e in entries if os.path.isdir(e)]
    if len(roots) != 1 or not os.path.exists(
            os.path.join(roots[0], "VERSION")):
        forms.alert("The downloaded package does not look like a PP Tools "
                    "release (no VERSION file found).", exitscript=True)
    new_root = roots[0]

    # remove current *.tab folders so pushbuttons deleted upstream also
    # disappear locally, then copy the new files in
    for entry in os.listdir(ext_root):
        full = os.path.join(ext_root, entry)
        if os.path.isdir(full) and entry.lower().endswith(".tab"):
            shutil.rmtree(full)
    copy_tree(new_root, ext_root)
finally:
    shutil.rmtree(staging, ignore_errors=True)

try:
    from pyrevit.loader import sessionmgr
    sessionmgr.reload_pyrevit()
except Exception:
    forms.alert("PP Tools was updated to version {0}.\n\n"
                "Automatic reload failed - please reload pyRevit manually "
                "(pyRevit tab > Reload).".format(remote_version))
