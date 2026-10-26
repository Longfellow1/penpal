# Releasing Penpal

Per-version notes live next to this file (`vX.Y.Z….md`). Before tagging a release commit:

1. Dispatch `.github/workflows/upgrade-drill.yml` on that commit and wait for both modes to pass:
   `upgrade` (previous public release → this commit through `install.sh`) and `fresh`.
   A red drill blocks the release; the drill installs through the real tap formula, so a dependency
   missing from the formula's resources fails here too.
2. Tag, publish the GitHub release, then point the tap formula (Longfellow1/homebrew-penpal) at the tag:
   `url`, `sha256`, `version`, and any changed `resource` blocks.
3. `clean-install.yml` runs on the published release and must turn green against the new formula.
