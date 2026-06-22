#!/usr/bin/env bash
# One-time Linux setup for osu!collector-gui's collection features.
#
# Reading existing osu!lazer collections (and merging into them) is done by the
# Collection Manager CLI — a Windows .NET 9 tool. On Linux it runs through the
# WineHQ flatpak, which needs the .NET 9 runtime added to its prefix. This
# script installs the flatpak, drops the .NET 9 runtime into its wine prefix,
# and grants the sandbox access to your osu! data. The app itself auto-downloads
# the CM CLI, so after running this once, collections just work.
#
# Downloads + auto-import into osu!lazer work WITHOUT this — it's only the
# collection list/merge that needs it.
#
# Usage:  scripts/setup-linux.sh  [path-to-osu-data-dir]
#         (defaults to ~/.local/share/osu, where client.realm lives)
set -euo pipefail

OSU_DATA="${1:-$HOME/.local/share/osu}"
APP_ID="org.winehq.Wine"
PREFIX="$HOME/.var/app/$APP_ID/data/wine"

command -v flatpak >/dev/null || { echo "flatpak is required. Install it first."; exit 1; }
flatpak remotes | grep -q flathub || \
  flatpak remote-add --if-not-exists --user flathub https://flathub.org/repo/flathub.flatpakrepo

echo "==> Installing the WineHQ flatpak (bundles wine + mono)..."
# org.winehq.Wine ships many branches; a bare install can't auto-pick one, so
# resolve the newest stable-* branch on flathub and pin it.
BRANCH=$(flatpak remote-ls flathub --columns=application,branch 2>/dev/null \
  | awk -v a="$APP_ID" '$1==a{print $2}' | grep '^stable-' | sort -V | tail -1)
BRANCH="${BRANCH:-stable-25.08}"
echo "    branch: $BRANCH"
flatpak install -y --user --noninteractive flathub "$APP_ID//$BRANCH"

echo "==> Initialising the wine prefix..."
flatpak run "$APP_ID" wineboot -u >/dev/null 2>&1 || true

echo "==> Installing the .NET 9 runtime into the wine prefix..."
DOTNET_DIR="$PREFIX/drive_c/Program Files/dotnet"
mkdir -p "$DOTNET_DIR"
META="https://builds.dotnet.microsoft.com/dotnet/release-metadata/9.0/releases.json"
dotnet_url() {  # $1 = section (runtime | windowsdesktop)
  curl -fsSL --max-time 30 "$META" | python3 -c "
import sys, json
rel = json.load(sys.stdin)['releases'][0]
for f in rel['$1']['files']:
    if f.get('rid') == 'win-x64' and f['name'].endswith('.zip'):
        print(f['url']); break
"
}
for section in runtime windowsdesktop; do
  url="$(dotnet_url "$section")"
  echo "    + $section: ${url##*/}"
  curl -fsSL --max-time 180 -o /tmp/oc-dotnet9.zip "$url"
  python3 -c "import zipfile,sys; zipfile.ZipFile('/tmp/oc-dotnet9.zip').extractall(sys.argv[1])" "$DOTNET_DIR"
done
rm -f /tmp/oc-dotnet9.zip

echo "==> Granting the wine sandbox access to your osu! data + the CM CLI cache..."
flatpak override --user --filesystem="$OSU_DATA" "$APP_ID"
flatpak override --user --filesystem="$HOME/.cache/osu-collector-gui" "$APP_ID"

echo
echo "==> Done. Launch osu!collector-gui — your collections will list, and"
echo "    merging into them will work. (osu! data dir used: $OSU_DATA)"
