#!/usr/bin/env bash
# Print the CHANGELOG.md section for a version; pre-releases fall back to their final version's section.
# Usage: release-notes.sh <version> [changelog]   e.g. release-notes.sh 0.1.0rc1
set -euo pipefail

version="${1:?usage: release-notes.sh <version> [changelog]}"
changelog="${2:-CHANGELOG.md}"

section() {
  awk -v head="## [$1]" '
    /^## \[/ || /^\[[^]]+\]: / { if (found) exit }
    found { lines[++n] = $0 }
    index($0, head) == 1 { found = 1 }
    END {
      first = 1; while (first <= n && lines[first] ~ /^[[:space:]]*$/) first++
      last = n;  while (last >= first && lines[last] ~ /^[[:space:]]*$/) last--
      for (i = first; i <= last; i++) print lines[i]
    }
  ' "$changelog"
}

notes="$(section "$version")"
base="$(sed -E 's/(a|b|rc)[0-9]+$//' <<<"$version")"
if [[ -z "$notes" && "$base" != "$version" ]]; then
  notes="$(section "$base")"
  [[ -n "$notes" ]] && notes="Pre-release of ${base}."$'\n\n'"$notes"
fi
if [[ -z "$notes" ]]; then
  echo "::error::no '## [$version]' section in $changelog" >&2
  exit 1
fi
printf '%s\n' "$notes"
