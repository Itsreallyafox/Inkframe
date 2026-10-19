# Waveshare 4inch e-Paper HAT+ (E) vendor driver (reference only)

These files are verbatim copies of the Waveshare driver for the 4inch e-Paper
HAT+ (E) (E Ink Spectra 6, controller frame 400 x 600 portrait). They are kept
for provenance and for checking the port against the original. **Nothing in
Inkframe imports them**, and they are never installed or executed. The
vendor `epdconfig.py` in particular has import-time side effects (it shells out
to `/proc/cpuinfo`, claims GPIO lines and drives pins) and must never be
imported.

The driver Inkframe actually runs is `inkframe/display/epd4in0e.py`: a trimmed
port of `epd4in0e.py` + `epdconfig.py` behind the `Display` protocol, with the
same wire sequence, injectable SPI/GPIO backends, BUSY timeouts and a
guaranteed power-down path. Pixel packing lives in
`inkframe/display/palette.py` (`pack_frame`), which reproduces the vendor
`getbuffer` byte layout.

## Origin

| | |
|---|---|
| Repository | https://github.com/waveshare/e-Paper |
| Commit | `a794fbc39656b0f93938d1ffb3fdc77eaed9e9fc` (2026-08-19) |
| Vendored on | 2026-10-19 |

| File here | Upstream path | Upstream blob |
|---|---|---|
| `epd4in0e.py` | `E-paper_Separate_Program/4inch_e-Paper_E/RaspberryPi_JetsonNano/python/lib/waveshare_epd/epd4in0e.py` | `ed0558432e629018bb90b1dc2f7e5b960de50d5e` |
| `epdconfig.py` | `E-paper_Separate_Program/4inch_e-Paper_E/RaspberryPi_JetsonNano/python/lib/waveshare_epd/epdconfig.py` | `b390252637b94026bc649fcfcaf911ec50ce4289` |
| `EPD_4in0e.c` | `E-paper_Separate_Program/4inch_e-Paper_E/RaspberryPi_JetsonNano/c/lib/e-Paper/EPD_4in0e.c` | `71bffc6146b3a9ce5980235c19331faf5701d00f` |
| `EPD_4in0e.h` | `E-paper_Separate_Program/4inch_e-Paper_E/RaspberryPi_JetsonNano/c/lib/e-Paper/EPD_4in0e.h` | `d29432ee6dd1cdf84d5dcca3acd65b48fb3974a8` |

The upstream blobs use LF line endings and the files here are byte-identical
to them (a Windows checkout with `core.autocrlf=true` shows CRLF; that was
stripped). `git hash-object <file>` on each file reproduces the blob id above.
The header comments are the vendor's, including their typos and the stale
`epd7in3f.py` file name in the Python driver's banner.

## Licence

The upstream repository has no LICENSE file. Each source file carries an
inline MIT-style permission notice in its header comment ("Permission is
hereby granted, free of charge, ..."); that notice is the licence for these
files and for the derived port in `inkframe/display/epd4in0e.py`, and it is
preserved unchanged here.

## Notes from reading the vendor code

These are the facts the port relies on; file:line refer to the files here.

- Pins (BCM): RST 17, DC 25, CS 8 (hardware CE0; the Python driver's software
  CS writes are no-ops), BUSY 24 (input, pull-down), PWR 18 (switches the
  HAT's 5 V rail). `epdconfig.py` 43-60.
- SPI `/dev/spidev0.0`, 4 MHz, mode 0. `epdconfig.py` 142-145.
- BUSY is active **low**: the panel is busy while BCM24 reads 0
  (`epd4in0e.py` 88-93, `EPD_4in0e.c` 78-86). The C function comment "Wait
  until the busy_pin goes LOW" is the opposite of what its code does; the code
  is right. The vendor loop has no timeout.
- The init sequence (`epd4in0e.py` 114-183, `EPD_4in0e.c` 120-188) and
  `TurnOnDisplay` (`epd4in0e.py` 95-112, `EPD_4in0e.c` 92-114) send the
  same bytes in both drivers. Timing differs slightly: the C driver waits an
  extra 200 ms after the power-on BUSY wait, and its BUSY loop polls every
  10 ms and settles 100 ms where the Python one polls every 5 ms and settles
  200 ms. The port uses the Python loop timing plus the C driver's extra
  200 ms (and a 200 ms settle after the power-off wait, as the vendor's ESP32
  variant does): a superset of both.
- Command `0x06` is sent twice with different last bytes (`0x17` in init,
  `0x27` in `TurnOnDisplay`); both are intentional and kept.
- Deep sleep is `0x07 0xA5` with no BUSY wait (commented out in
  `EPD_4in0e.c` 297), then at least 2 s before cutting 5 V (vendor C test:
  "important, at least 2s"), then SPI close, RST/DC/PWR low
  (`epdconfig.py` 148-154). After deep sleep only a full re-init wakes the
  controller.
- Colour codes (`EPD_4in0e.h` 43-48): black 0x0, white 0x1, yellow 0x2,
  red 0x3, blue 0x5, green 0x6.
