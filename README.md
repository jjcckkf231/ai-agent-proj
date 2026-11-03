# EDU PICO V2 Voice Assistant

This project implements the following loop:

1. The PAJ7620 detects a nearby hand.
2. The EDU PICO V2 records up to five seconds from its PDM microphone.
3. A desktop Python agent receives the audio over a dedicated USB serial port.
4. PocketSphinx transcribes the WAV locally on the computer.
5. The agent sends the short transcript to a local Qwen3 model through Ollama.
6. The OLED pages through the answer and the buzzer plays two notification tones.
7. The final answer remains frozen on the OLED until Button A is pressed.
8. The board reports completion and returns to `Place hand to ask`.

## Hardware mapping

| Device | EDU PICO V2 connection |
|---|---|
| PAJ7620 proximity sensor | I2C: SDA GP4, SCL GP5 |
| SSD1315 OLED | I2C: SDA GP4, SCL GP5 |
| MP34DT05-A PDM microphone | DATA GP2, CLK GP3 |
| Piezo buzzer | GP21 |

Move the physical audio switch to **BUZZER**.

## 1. Install the board firmware

1. Install the current EDU PICO 2 for Pico 2 W CircuitPython firmware from
   <https://circuitpython.org/board/cytron_edu_v2_pico_2w/>.
2. Copy `pico/boot.py` and `pico/code.py` to the root of the `CIRCUITPY` drive.
3. Safely eject and reconnect the board. `boot.py` creates a second USB CDC
   port dedicated to binary audio. A restart is required after changing it.

The official V2 firmware already bundles `adafruit_ssd1306` and
`edupico2_paj7620`. If imports fail, verify that the board has the V2 build,
not the older EDU PICO firmware.

## 2. Install the desktop agent

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python agent\main.py --port COM4
```

Replace `COM4` if the board's **data** serial port is different in Device Manager.
If automatic discovery sees exactly one CircuitPython port, `--port` can be
omitted. The baud value is part of the serial API but USB CDC ignores it.

Install Ollama from <https://ollama.com/download/windows>, then download the
local model once:

```powershell
ollama pull qwen3:1.7b
python agent\main.py --port COM4
```

The default model is Qwen3 1.7B (about 1.4 GB, Apache 2.0). To use another
installed Ollama model, pass `--model model-name` or set `OLLAMA_MODEL`.

### Local privacy

Voice recordings, PocketSphinx transcripts, and model answers remain on the
local computer. The assistant requires no cloud API key and consumes no API
tokens. Network access is only needed to install Ollama and download the model.

PocketSphinx's bundled recognizer is designed for US English. It is fully
offline and token-free but will be less accurate than modern neural speech
recognition, particularly for other languages, strong accents, or noisy rooms.

## 3. Calibrate the two thresholds

The first values to tune in `pico/code.py` are:

```python
PROXIMITY_TRIGGER = 150
SILENCE_AMPLITUDE = 65
```

- Raise `PROXIMITY_TRIGGER` if the system starts without a nearby hand; lower
  it if a hand does not trigger reliably.
- Raise `SILENCE_AMPLITUDE` in a noisy room; lower it if quiet speech is cut
  off. Recording always stops at five seconds even if silence detection fails.

The included defaults were measured on the initial test board: no-hand
proximity was `0`, a hand at 5 cm was `253-255`, room-silence amplitude was
usually `35-60`, and ordinary speech produced voiced peaks around `70-163`.

## OLED language limitation

The built-in OLED font contains basic ASCII glyphs. The desktop agent therefore
normalizes answers to ASCII before sending them. Supporting Thai or other
scripts requires adding a suitable bitmap font and a display-text renderer.

## Test the desktop logic

The tests do not contact the API or require a connected board:

```powershell
python -m pip install pytest
python -m pytest -q
```

## Troubleshooting

- **No data port:** reconnect the board after copying `boot.py`.
- **No buzzer:** move the audio/buzzer switch to BUZZER.
- **Sensor import error:** install the official EDU PICO V2 CircuitPython build.
- **No trigger:** calibrate `PROXIMITY_TRIGGER` using the V2 proximity example.
- **Audio is silent/noisy:** confirm microphone DATA=GP2 and CLK=GP3, then tune
  `SILENCE_AMPLITUDE`.

For measured calibration, temporarily copy `pico/calibrate.py` to the board as
`code.py`. It prints records in the form `CAL,p=<proximity>,a=<amplitude>` to
the CircuitPython console. Restore `pico/code.py` after choosing the thresholds.
- **Ollama error on OLED:** confirm Ollama is running and that `ollama list`
  shows the configured local model.

