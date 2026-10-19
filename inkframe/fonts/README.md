# Bundled fonts

Inkframe renders its cards with Pillow and must never depend on system
fonts, so three SIL Open Font License 1.1 families are bundled here as
**static TrueType instances** (no variable fonts, so Pillow needs no
variation API). `fonts.json` is the machine-readable index the app loads;
the roles below match its keys.

| Role  | Family           | Version | Licence | Source |
|-------|------------------|---------|---------|--------|
| serif | Playfair Display | 1.202   | OFL-1.1 | https://github.com/clauseggers/Playfair-Display (tag `1.202`, `fonts/TTF/`) |
| sans  | Inter            | v4.1    | OFL-1.1 | https://github.com/rsms/inter/releases/tag/v4.1 (`Inter-4.1.zip`, `extras/ttf/`) |
| mono  | JetBrains Mono   | v2.304  | OFL-1.1 | https://github.com/JetBrains/JetBrainsMono/releases/tag/v2.304 (`JetBrainsMono-2.304.zip`, `fonts/ttf/`) |

## Files

- `playfair-display/` (serif: display serif for quotes and big numerals)
  - `PlayfairDisplay-Regular.ttf`, `PlayfairDisplay-Bold.ttf`, `PlayfairDisplay-Italic.ttf`
  - `OFL.txt`
- `inter/` (sans: UI text for weather and headlines)
  - `Inter-Regular.ttf`, `Inter-Medium.ttf`, `Inter-SemiBold.ttf`, `Inter-Bold.ttf`
  - `OFL.txt` (Inter's `LICENSE.txt`, which is the OFL 1.1 text)
- `jetbrains-mono/` (mono: small labels, times, attributions)
  - `JetBrainsMono-Regular.ttf`, `JetBrainsMono-Bold.ttf`
  - `OFL.txt`

## Notes

- Playfair Display is pinned to the 1.202 tag because the current 2.x
  releases of the same repository (now named `clauseggers/Playfair`) ship
  only variable fonts (checked: 2.203 and 2.100-RC-1). 1.202 is the newest tag with static TTFs.
- The font binaries are unmodified upstream files. Do not rename or
  subset them without checking the Reserved Font Name clauses in each
  `OFL.txt`.
- To add a weight: copy the static `.ttf` from the same upstream release
  into the family directory and add it to `files` in `fonts.json`.
  `regular` and `bold` are required for every role.
