# Scrub-DICOM brand

One mark, one wordmark, four colours. Everything is generated from `packaging/icons/make_icons.py`, so the
icon set is reproducible and nobody has to redraw it.

## The mark

A hand-drawn flat paintbrush sweeping across the CT gantry ring on a navy tile: solid brown handle with a
faint grain and a wobbly ink outline, a silver ferrule flecked with paint, ink bristles that bend together into
a tapered edge, and a loose yellow smear trailing from the edge. It is deliberately sketchy, a human hand
cleaning a scan, and it is drawn procedurally with a fixed seed so every build produces the same pixels.

Do not add text to the mark, a heart or a shield; do not straighten the brush into a geometric symbol; do not
change the yellow of the smear or the navy of the tile. The one-colour glyph is the ring alone.

## Palette

| Name | Hex | Use |
|---|---|---|
| Navy | `#0E3A5C` | tile, header cards |
| Navy deep | `#0C2642` | wordmark "Scrub", step titles |
| Teal | `#00BAA8` | the clean sweep, wordmark "DICOM", success and accent in the app |
| Amber | `#FFB22E` | warnings in the status strip |
| Yellow | `#FAE128` | the paint smear (mark only) |
| Slate | `#5E6C7E` | the slice |
| Off-white | `#F0F4F8` | ferrule, light tile |
| Ink | `#111827` | bristles |

## Wordmark

"Scrub" in navy and "DICOM" in teal, Helvetica Neue Bold (or Inter Bold where Helvetica Neue is not
available), set on one line with a single space between the words. The mark sits to the left at the height of
the capitals, with a gap equal to half the mark's width. Never set the wordmark in the sketchy style or in a
serif.

## Sizes and files

`make_icons.py` writes `icon.png` (1024), `icon.ico` (16 to 256), `icon.icns` (16 to 1024 at 1x and 2x)
and `scrubdicom/app/assets/mark_<px>.png` at 24 to 192 px for the app's header and About box (each size is
resampled by the generator; Tk never scales an image, which would pixelate it). `python packaging/icons/make_icons.py --sheet` produces a
review sheet showing every size, the light variant, the glyph and the wordmark.

## Where it appears

- macOS: the app icon and the DMG.
- Windows: the executable and the installer.
- In the app: the header (mark + wordmark, teal rule beneath), the Home page (mark, wordmark, three coloured
  cards: navy Start, teal Try it, slate Viewer), teal step indicators, and the About box.
- Documents and slides use the one-colour glyph in navy, or in white on navy.
