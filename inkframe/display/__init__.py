"""Display layer: palette, the Display protocol, the mock and the real panel.

Select with ``INKFRAME_DISPLAY=mock|epd`` (default ``mock``).  ``epd`` is only
importable on the Pi (it needs ``spidev`` and ``gpiozero``); nothing in this
package imports it unless asked to, so desktop tests never touch GPIO.
"""
