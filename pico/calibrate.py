"""Temporary EDU PICO V2 sensor calibration program.

Copy this file to CIRCUITPY/code.py, watch the USB console, then restore the
assistant code.py after recording no-hand, near-hand, speech, and silence data.
"""

import array
import time

import audiobusio
import board
import busio
import edupico2_paj7620


SAMPLE_RATE = 32_000
CHUNK_SAMPLES = 1024

i2c = busio.I2C(board.GP5, board.GP4)
sensor = edupico2_paj7620.PAJ7620(i2c)
microphone = audiobusio.PDMIn(
    board.GP3,
    board.GP2,
    sample_rate=SAMPLE_RATE,
    bit_depth=16,
    mono=True,
)
chunk = array.array("H", [0] * CHUNK_SAMPLES)


def read_proximity():
    for name in ("proximity", "proximity_data", "get_proximity"):
        try:
            value = getattr(sensor, name)
            if callable(value):
                value = value()
            return int(value)
        except (AttributeError, TypeError, ValueError):
            pass
    raise RuntimeError("PAJ7620 driver has no supported proximity accessor")


def amplitude(samples, count):
    if count <= 0:
        return 0
    mean = sum(samples[:count]) // count
    total = 0
    for index in range(count):
        total += abs(samples[index] - mean)
    return total // count


print("CALIBRATION_READY")
while True:
    count = microphone.record(chunk, CHUNK_SAMPLES)
    if count <= 0:
        continue
    values = chunk[:count]
    print(
        "CAL,p={},a={},min={},max={}".format(
            read_proximity(), amplitude(chunk, count), min(values), max(values)
        )
    )
    time.sleep(0.05)

