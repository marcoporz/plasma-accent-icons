# Plasma Accent Icons

Generate a KDE icon theme that recolors the whole desktop — including icons
missing from the base icon pack and icons of later-installed applications — in
a color taken from the current Plasma palette. The default is the light window
text color, the same tone used by the clock and system-monitor panel labels.

The generated theme is called **Plasma-Accent**. It is based on
[YAMIS](https://bitbucket.org/dirn-typo/yet-another-monochrome-icon-set)
(`yet-another-monochrome-icon-set`) and is a separate theme: YAMIS itself is
never modified.

## What it does

* Copies YAMIS and rewrites its `ColorScheme-Text` / `ColorScheme-*` icon
  classes to the selected palette class. The default is `ColorScheme-Text`
  (light panel text); `ColorScheme-Accent` is available as an option.
* Sets `FollowsColorScheme=true` in the generated theme, so KDE substitutes
  the palette color at load time. Changing the palette does **not** require
  rebuilding the theme.
* Adds icon names that YAMIS does not contain, using this fallback order:
  1. `plasma-monochrome-icons`
  2. `breeze` (or `breeze-dark`)
  3. `Adwaita`
  4. `hicolor` from the user, system, Flatpak and Snap icon roots
* Patches monochrome SVGs in place. Colored SVGs and raster images are turned
  into a luminance mask filled with `currentColor` + the selected palette
  class, so they become monochrome icons instead of staying colored.
* Keeps a manifest inside the theme so `sync` can add icons for newly
  installed applications without rebuilding everything.
* Ships a systemd user timer that runs `sync` periodically.

This is full monochrome recoloring, not just accent details on folders.

## Requirements

* Plasma 6 / KDE Frameworks 6 (`KIconThemes`, `kwriteconfig6`,
  `kbuildsycoca6`).
* Python 3.9 or newer.
* [`numpy`](https://numpy.org/) and
  [`Pillow`](https://python-pillow.org/) for mask generation.
* `rsvg-convert` (librsvg) or ImageMagick's `magick` command for rendering
  SVGs to masks.
* The YAMIS icon theme installed in one of the XDG icon directories. The
  default source is `~/.local/share/icons/YAMIS`.

On Arch Linux:

```bash
sudo pacman -S python-numpy python-pillow librsvg
```

## Quick start

```bash
git clone https://github.com/marcoporz/plasma-accent-icons.git
cd plasma-accent-icons

# Build and install ~/.local/share/icons/Plasma-Accent
python3 accent_icons.py install

# Inspect coverage and check for missing application icons
python3 accent_icons.py status

# Switch the KDE icon theme (only when you are ready)
python3 accent_icons.py activate
```

After activation, restart `plasmashell` or log out and back in. Applications
that keep their own icon cache may need to be restarted as well.

To go back to the previous theme:

```bash
kwriteconfig6 --file kdeglobals --group Icons --key Theme YAMIS
kbuildsycoca6 --noincremental
```

## Commands

| Command | Purpose |
| --- | --- |
| `python3 accent_icons.py build [--output DIR] [--jobs N] [--color MODE]` | Build the theme in a directory without installing it. |
| `python3 accent_icons.py install [--activate] [--color MODE]` | Build and atomically replace the installed theme. |
| `python3 accent_icons.py sync [--dry-run] [--color MODE]` | Add/update icons for new applications and remove stale generated icons. Without `--color` it keeps the installed mode. |
| `python3 accent_icons.py status` | Show icon counts, sources, unresolved names and desktop-icon coverage. |
| `python3 accent_icons.py activate` | Set `Icons/Theme=Plasma-Accent` and refresh the KDE cache. |
| `python3 accent_icons.py install-units [--enable]` | Install `~/.local/bin/plasma-accent-icons` and the systemd user units. `--enable` starts the timer. |
| `python3 accent_icons.py uninstall [--units]` | Remove the theme; `--units` also removes the helper and timer. |
| `python3 accent_icons.py clean [--all]` | Remove the mask/build cache. |

`sync` is incremental: it compares source mtimes and the base-theme
fingerprint. If YAMIS or the color mode changes, it automatically falls back to
a full rebuild. Deleting a generated icon and running `sync` restores it.

Useful examples:

```bash
# See what sync would change
python3 accent_icons.py sync --dry-run

# Use a different base theme or output directory
python3 accent_icons.py build --base breeze --output /tmp/Plasma-Accent-test

# Rebuild without reusing cached masks
python3 accent_icons.py install --no-cache

# Use the darker selection/accent tone instead of the text tone
python3 accent_icons.py install --color accent --activate
```

## Automatic synchronization

Install the helper and timer:

```bash
python3 accent_icons.py install-units --enable
```

This installs:

* `~/.local/bin/plasma-accent-icons`
* `~/.config/systemd/user/plasma-accent-icons-sync.service`
* `~/.config/systemd/user/plasma-accent-icons-sync.timer`

The timer runs `sync` two minutes after login and then every 15 minutes.
Check it with:

```bash
systemctl --user list-timers plasma-accent-icons-sync.timer
systemctl --user status plasma-accent-icons-sync.service
journalctl --user -u plasma-accent-icons-sync.service
```

The timer is **not** enabled by default; `--enable` is required. You can also
run `plasma-accent-icons sync --quiet` manually or from a package-manager hook
for immediate updates.

## Color behavior

KDE's icon loader supports a small set of color classes in SVG files
(`ColorScheme-Text`, `ColorScheme-Accent`, and a few others). When the theme
has `FollowsColorScheme=true`, KIconLoader replaces the theme's
`current-color-scheme` style block with the active palette colors.

Two modes are available:

* `text` (default) uses `ColorScheme-Text`, which KDE resolves to
  `QPalette::WindowText`. In a Material You dark scheme this is the same light
  tone used by the clock, KVitals and other panel labels, so icons blend in
  with the panel typography.
* `accent` uses `ColorScheme-Accent`, the tone intended for accent-colored
  icons. KDE derives it from the palette accent, which is usually the
  selection background: a darker, more saturated shade than the panel text.

Select the mode at build/install time with `--color text` or
`--color accent`. The mode is stored in the manifest; `sync` keeps it unless
you pass `--color` explicitly, and switching mode triggers a full rebuild.
Because the theme follows the palette at load time, later palette changes
still apply immediately without rebuilding.

## What can and cannot be themed

Themed:

* Icons resolved through KDE's icon theme system, including application
  launcher icons, Dolphin, Plasma widgets and symbolic action icons.
* Icons provided by apps under `hicolor`, Flatpak and Snap export roots.
* Raster icons from those themes, converted to accent masks.

Not themed:

* Hard-coded absolute icon paths in `.desktop` files (for example an icon
  under `/usr/share/hplip/...`). The icon loader does not resolve these
  through a theme.
* Icons painted inside an application's own UI, web favicons, game assets and
  similar non-theme artwork.
* GTK applications that use a separate GTK icon theme and bypass KDE's icon
  loader.
* Icons for applications installed after the last `sync` run, until the timer
  or a manual `sync` adds them. Until then KDE falls back to `hicolor` and the
  icon may appear in color.

`status` lists desktop icon names that have no themed file and absolute-path
icons that cannot be themed.

## Files and cache

| Path | Meaning |
| --- | --- |
| `~/.local/share/icons/Plasma-Accent/` | Installed theme. |
| `~/.local/share/icons/Plasma-Accent/.plasma-accent-manifest.json` | Generated-icon manifest used by `sync`. |
| `~/.cache/plasma-accent-icons/masks/` | Content-addressed mask cache. Safe to delete. |
| `~/.local/bin/plasma-accent-icons` | Helper installed by `install-units`. |
| `~/.config/systemd/user/plasma-accent-icons-sync.*` | systemd user units. |

The generator only reads YAMIS, Breeze, Adwaita, hicolor and the other source
themes. It never writes to them.

## Development

```bash
# Build a throwaway copy
python3 accent_icons.py build --output /tmp/Plasma-Accent-build --jobs 8

# Verbose progress
python3 accent_icons.py build --output /tmp/Plasma-Accent-build --verbose
```

The mask cache key includes a content hash of the source file, the color mode
and `MASK_VERSION` in `accent_icons.py`. Bump `MASK_VERSION` after changing the
mask algorithm, or pass `--no-cache`.

## License and attribution

The generator and the generated theme are distributed under **GPL-3.0**
because the theme is derived from YAMIS, which is GPL-3.0. See `LICENSE` and
`NOTICE.md`. Third-party icon sets included in the generated theme keep their
own licenses; `NOTICE.md` lists the sources and the corresponding license
texts are in `licenses/`.
