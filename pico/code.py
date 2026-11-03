"""EDU PICO V2 voice-assistant firmware (CircuitPython 10.x).

Hardware used:
  PAJ7620 proximity sensor and SSD1315 OLED: I2C, SCL GP5 / SDA GP4
  MP34DT05-A PDM microphone: clock GP3 / data GP2
  Piezo buzzer: GP21 (set the physical audio switch to BUZZER)
"""

import array
import json
import math
import time

import adafruit_ssd1306
import audiobusio
import board
import busio
import digitalio
import edupico2_paj7620
import pwmio
import usb_cdc


MIC_SAMPLE_RATE = 32_000
OUTPUT_SAMPLE_RATE = 16_000
MAX_RECORD_SECONDS = 5.0
MIN_RECORD_SECONDS = 1.0
SILENCE_SECONDS = 0.9
CHUNK_SAMPLES = 1024

# Tune these two values while watching proximity/audio diagnostics in the REPL.
PROXIMITY_TRIGGER = 150
SILENCE_AMPLITUDE = 65

OLED_WIDTH = 128
OLED_HEIGHT = 64
OLED_COLUMNS = 21
OLED_LINES = 7
PAGE_SECONDS = 3.0


i2c = busio.I2C(board.GP5, board.GP4)
oled = adafruit_ssd1306.SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c)
proximity_sensor = edupico2_paj7620.PAJ7620(i2c)
microphone = audiobusio.PDMIn(
    board.GP3,
    board.GP2,
    sample_rate=MIC_SAMPLE_RATE,
    bit_depth=16,
    mono=True,
)
buzzer = pwmio.PWMOut(
    board.GP21, frequency=880, duty_cycle=0, variable_frequency=True
)
button_a = digitalio.DigitalInOut(board.GP0)
button_a.switch_to_input(pull=digitalio.Pull.UP)
serial = usb_cdc.data


def show_lines(lines):
    oled.fill(0)
    for index, line in enumerate(lines[:OLED_LINES]):
        oled.text(line[:OLED_COLUMNS], 0, index * 9, 1)
    oled.show()


def show_message(*lines):
    show_lines(list(lines))


def wrap_text(text):
    """Wrap plain ASCII text for the OLED without splitting words when possible."""
    words = text.replace("\r", " ").replace("\n", " ").split()
    lines = []
    current = ""
    for word in words:
        while len(word) > OLED_COLUMNS:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:OLED_COLUMNS])
            word = word[OLED_COLUMNS:]
        candidate = word if not current else current + " " + word
        if len(candidate) <= OLED_COLUMNS:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or ["(empty answer)"]


def beep():
    for frequency in (880, 1175):
        buzzer.frequency = frequency
        buzzer.duty_cycle = 32768
        time.sleep(0.12)
        buzzer.duty_cycle = 0
        time.sleep(0.08)


def wait_for_button_a():
    """Keep the final answer visible until Button A is pressed and released."""
    while button_a.value:
        time.sleep(0.02)
    time.sleep(0.05)  # Debounce the press.
    while not button_a.value:
        time.sleep(0.02)
    time.sleep(0.05)  # Debounce the release.


def read_proximity():
    """Read V2 proximity while tolerating minor driver API differences."""
    for name in ("proximity", "proximity_data", "get_proximity"):
        try:
            value = getattr(proximity_sensor, name)
            if callable(value):
                value = value()
            return int(value)
        except (AttributeError, TypeError, ValueError):
            pass
    raise RuntimeError("PAJ7620 driver has no supported proximity accessor")


def wait_for_trigger():
    show_message("Place hand to ask")
    consecutive = 0
    while True:
        try:
            value = read_proximity()
            consecutive = consecutive + 1 if value >= PROXIMITY_TRIGGER else 0
            if consecutive >= 3:
                while read_proximity() >= PROXIMITY_TRIGGER:
                    time.sleep(0.05)
                return
        except Exception as error:  # Keep the error visible instead of reboot-looping.
            show_message("Sensor error", str(error))
            time.sleep(2)
        time.sleep(0.05)


def chunk_amplitude(samples, count):
    if count <= 0:
        return 0
    mean = sum(samples[:count]) // count
    total = 0
    for index in range(count):
        total += abs(samples[index] - mean)
    return total // count


def record_question():
    """Record until sustained silence after speech, or the five-second limit."""
    show_message("Listening...", "Speak now")
    audio = array.array("H")
    chunk = array.array("H", [0] * CHUNK_SAMPLES)
    started = time.monotonic()
    silent_since = None
    heard_speech = False

    while time.monotonic() - started < MAX_RECORD_SECONDS:
        count = microphone.record(chunk, CHUNK_SAMPLES)
        # The EDU PICO V2 microphone is reliable at 32 kHz. Keep every second
        # sample so five seconds of 16 kHz audio fits comfortably in memory.
        for index in range(0, count, 2):
            audio.append(chunk[index])
        elapsed = time.monotonic() - started
        amplitude = chunk_amplitude(chunk, count)

        if amplitude >= SILENCE_AMPLITUDE:
            heard_speech = True
            silent_since = None
        elif heard_speech and elapsed >= MIN_RECORD_SECONDS:
            if silent_since is None:
                silent_since = time.monotonic()
            elif time.monotonic() - silent_since >= SILENCE_SECONDS:
                break

    return audio


def write_all(data):
    view = memoryview(data)
    sent = 0
    while sent < len(view):
        # Small writes are more reliable than one large transfer on Windows CDC.
        count = serial.write(view[sent : min(sent + 1024, len(view))])
        if count:
            sent += count


def send_audio(samples):
    # Samples are unsigned 16-bit values. The desktop converts them to signed WAV PCM.
    header = "AUDIO {} {} U16LE\n".format(
        len(samples) * 2, OUTPUT_SAMPLE_RATE
    )
    write_all(header.encode("ascii"))
    write_all(memoryview(samples).cast("B"))


def read_json_command(timeout=45):
    deadline = time.monotonic() + timeout
    line = bytearray()
    while time.monotonic() < deadline:
        if serial.in_waiting:
            byte = serial.read(1)
            if byte == b"\n":
                if line:
                    return json.loads(line.decode("utf-8"))
            else:
                line.extend(byte)
        else:
            time.sleep(0.01)
    raise RuntimeError("Desktop agent timed out")


def display_answer(text):
    lines = wrap_text(text)
    pages = [lines[i : i + OLED_LINES] for i in range(0, len(lines), OLED_LINES)]
    beep()
    for page_number, page in enumerate(pages):
        if len(pages) > 1:
            page[-1:] = [(page[-1] if page else "")[:15] + " {}/{}".format(page_number + 1, len(pages))]
        show_lines(page)
        if page_number < len(pages) - 1:
            time.sleep(PAGE_SECONDS)
    wait_for_button_a()


def send_event(event, **fields):
    payload = {"event": event}
    payload.update(fields)
    write_all((json.dumps(payload) + "\n").encode("utf-8"))


def run_once():
    wait_for_trigger()
    samples = record_question()
    show_message("Uploading audio...")
    send_audio(samples)
    show_message("Processing...")

    while True:
        command = read_json_command()
        command_name = command.get("cmd")
        if command_name == "status":
            show_message(str(command.get("text", "Processing...")))
        elif command_name == "answer":
            display_answer(str(command.get("text", "No answer")))
            send_event("done")
            return
        elif command_name == "error":
            show_message("Error", str(command.get("text", "Unknown error")))
            beep()
            time.sleep(3)
            send_event("done", ok=False)
            return


show_message("Voice Assistant", "Starting...")
time.sleep(1)
while True:
    try:
        run_once()
    except Exception as error:
        show_message("System error", str(error))
        time.sleep(3)

