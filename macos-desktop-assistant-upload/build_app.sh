#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
app_dir="$project_dir/build/My Planner.app"
mkdir -p "$app_dir/Contents/MacOS"
cp "$project_dir/MyPlannerInfo.plist" "$app_dir/Contents/Info.plist"
clang -fobjc-arc -O2 \
  -framework Cocoa -framework CoreGraphics \
  "$project_dir/MyPlannerCompanion.m" \
  -o "$app_dir/Contents/MacOS/MyPlanner"
printf '%s\n' "$app_dir"
