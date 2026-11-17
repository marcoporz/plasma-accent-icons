# Notice and attribution

`plasma-accent-icons` generates the **Plasma-Accent** icon theme. The
generated theme is an aggregate/derivative of the icon sets below. The
generator and the generated theme are distributed under **GPL-3.0** because
the base theme, YAMIS, is GPL-3.0. The full GPL-3.0 text is in `LICENSE`.

This project is not affiliated with or endorsed by the authors of YAMIS,
KDE, GNOME, the Orion icon theme, or any application whose icon is included.

## Base theme

**YAMIS — yet-another-monochrome-icon-set**  
Author: Dirn  
Upstream: <https://bitbucket.org/dirn-typo/yet-another-monochrome-icon-set>  
License: GPL-3.0 (see the `LICENSE` file copied from the installed theme)

The generated theme copies YAMIS artwork and rewrites its color-scheme
classes. YAMIS remains unmodified in its own directory.

## Fallback icon sources

**plasma-monochrome-icons**  
Author: Dirn  
Based on the **Orion icon theme** by Seth Storm Rosenaa:
<https://github.com/SethStormR/Orion>  
The installed copy used by this tool does not bundle a license file. Refer to
the upstream project for its licensing terms before redistributing the
generated theme.

**Breeze / Breeze Dark** (KDE Frameworks)  
Upstream: <https://invent.kde.org/frameworks/breeze-icons>  
License: LGPL-3.0-or-later and LGPL-2.1-only (as reported by the
`breeze-icons` package). License texts:
`licenses/LGPL-3.0-or-later.txt`, `licenses/LGPL-2.1-only.txt`.

**Adwaita icon theme** (GNOME)  
Upstream: <https://gitlab.gnome.org/GNOME/adwaita-icon-theme>  
License: CC-BY-SA-3.0 OR LGPL-3.0-only. License texts:
`licenses/CC-BY-SA-3.0.txt`, `licenses/LGPL-3.0-or-later.txt`.

**hicolor icon theme and application icons**  
Upstream: <https://gitlab.freedesktop.org/xdg/default-icon-theme>  
The hicolor theme package is GPL-2.0-or-later
(`licenses/GPL-2.0-or-later.txt`). Individual application icons installed
under `hicolor`, `/usr/share/pixmaps`, Flatpak exports, Snap exports or
application-specific directories remain the property of their respective
authors and keep their original licenses. They are not relicensed by this
project. Check the corresponding application package before redistributing
the generated theme.

## Generated files

All SVGs and raster-derived masks in `~/.local/share/icons/Plasma-Accent/`
are generated from the sources above. The manifest
`.plasma-accent-manifest.json` records the exact source path of every
generated icon. If an icon is redistributed, keep its source attribution and
license with it.

## Tool

`accent_icons.py` is part of this project and is distributed under GPL-3.0.
