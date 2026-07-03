# PP Tools

A pyRevit extension for Revit 2026 with the PP Tools ribbon tab
(Define, Family Types, Capture, and Setup panels).

## Requirements

- [pyRevit](https://github.com/pyrevitlabs/pyRevit/releases) installed
  for the current user.

## One-time publishing setup (extension owner)

1. Create a GitHub repository for this extension (public is simplest;
   the installer and updater download without authentication). Example
   name: `pp-tools`.
2. Edit the repo name in **two places**, replacing
   `your-github-username/pp-tools` with your actual `owner/repo`:
   - `PP Tools.tab/Setup.panel/Update PP Tools.pushbutton/script.py`
     (`GITHUB_REPO` at the top)
   - `installer/install.ps1` (`$GithubRepo` at the top)
3. Push this folder to the repository:

   ```
   cd "%APPDATA%\pyRevit\Extensions\PPTools.extension"
   git init
   git add .
   git commit -m "PP Tools 1.0.0"
   git branch -M main
   git remote add origin https://github.com/<owner>/<repo>.git
   git push -u origin main
   ```

## Installing (team members)

1. Install pyRevit if not already installed.
2. Get the `installer` folder (both `install.bat` and `install.ps1`)
   and double-click `install.bat`.
3. Start Revit - the **PP Tools** tab appears on the ribbon.

## Releasing an update (extension owner)

1. Make your changes to the tools.
2. Bump the version number in the `VERSION` file (e.g. `1.0.0` -> `1.1.0`).
3. Commit and push to `main`.

## Updating (team members)

Click **PP Tools tab > Setup panel > Update PP Tools**. The button
compares the installed `VERSION` with the one on GitHub; if a newer
version exists it downloads it, replaces the local files, and reloads
pyRevit automatically.
