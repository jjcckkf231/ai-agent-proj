"""Desktop bridge: EDU PICO audio -> local speech recognition/LLM -> OLED."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import struct
import sys
import time
import unicodedata
import wave
from dataclasses import dataclass

import serial
import requests
from pocketsphinx import Decoder
from serial.tools import list_ports


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:1.7b"
MAX_OLED_CHARACTERS = 180


@dataclass(frozen=True)
class AudioHeader:
    byte_count: int
    sample_rate: int
    encoding: str


def parse_audio_header(line: bytes) -> AudioHeader:
    parts = line.decode("ascii").strip().split()
    if len(parts) != 4 or parts[0] != "AUDIO":
        raise ValueError(f"Unexpected board message: {line!r}")
    header = AudioHeader(int(parts[1]), int(parts[2]), parts[3])
    if header.byte_count <= 0 or header.byte_count > 400_000:
        raise ValueError("Audio byte count is outside the safe range")
    if header.sample_rate not in (8_000, 16_000, 22_050, 24_000):
        raise ValueError("Unexpected audio sample rate")
    if header.encoding != "U16LE":
        raise ValueError("Unsupported audio encoding")
    return header


def read_exact(port: serial.Serial, byte_count: int) -> bytes:
    data = bytearray()
    while len(data) < byte_count:
        remaining = byte_count - len(data)
        chunk = port.read(min(4096, remaining))
        if not chunk:
            raise TimeoutError(
                "Timed out while receiving audio from the board "
                f"({len(data)}/{byte_count} bytes received)"
            )
        data.extend(chunk)
    return bytes(data)


def wait_for_audio_header(port: serial.Serial) -> AudioHeader:
    """Wait through idle serial timeouts until the board starts an upload."""
    while True:
        line = port.readline()
        if not line:
            continue
        if line.lstrip().startswith(b"{"):
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            else:
                if message.get("event") == "done":
                    continue
        return parse_audio_header(line)


def unsigned_u16_to_wav(raw: bytes, sample_rate: int) -> io.BytesIO:
    if len(raw) % 2:
        raise ValueError("16-bit audio contains an odd number of bytes")
    sample_count = len(raw) // 2
    unsigned_samples = struct.unpack(f"<{sample_count}H", raw)
    # CircuitPython/PDM implementations may use different unsigned DC offsets.
    # Removing the recording's measured mean works for both zero- and mid-biased data.
    dc_offset = sum(unsigned_samples) // max(1, sample_count)
    centered = [value - dc_offset for value in unsigned_samples]
    peak = max((abs(value) for value in centered), default=0)
    gain = min(64.0, 12_000 / peak) if peak else 1.0
    signed_pcm = struct.pack(
        f"<{sample_count}h",
        *(max(-32768, min(32767, int(value * gain))) for value in centered),
    )
    wav_file = io.BytesIO()
    with wave.open(wav_file, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(signed_pcm)
    wav_file.seek(0)
    wav_file.name = "question.wav"
    return wav_file


def oled_safe_text(text: str, limit: int = MAX_OLED_CHARACTERS) -> str:
    """Normalize to the ASCII glyphs supported by the OLED's built-in font."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    compact = " ".join(ascii_text.split())
    if not compact:
        return "I could not display that answer using the OLED font."
    if len(compact) <= limit:
        return compact
    shortened = compact[: limit - 3].rsplit(" ", 1)[0]
    return (shortened or compact[: limit - 3]) + "..."


def send_command(port: serial.Serial, command: str, **fields: object) -> None:
    payload = {"cmd": command, **fields}
    port.write((json.dumps(payload, ensure_ascii=True) + "\n").encode("ascii"))
    port.flush()


def wait_for_board_done(port: serial.Serial) -> None:
    """Wait indefinitely for Button A to release the frozen final answer."""
    while True:
        line = port.readline()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid completion message from board: {line!r}") from error
        if message.get("event") == "done":
            return
        raise ValueError(f"Unexpected board event: {message!r}")


def discover_port(explicit_port: str | None) -> str:
    if explicit_port:
        return explicit_port
    candidates = []
    for info in list_ports.comports():
        description = (info.description or "").lower()
        manufacturer = (info.manufacturer or "").lower()
        if "circuitpython" in description or "circuitpython" in manufacturer:
            candidates.append(info.device)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError("No CircuitPython data port found; use --port COMx")
    raise RuntimeError("Multiple CircuitPython ports found; use --port COMx")


def parse_model_output(content: str) -> tuple[str, str]:
    """Extract the requested transcript and answer, tolerating extra model text."""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    transcript_match = re.search(
        r"(?:^|\n)TRANSCRIPT:\s*(.*?)(?=\nANSWER:|$)",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    answer_match = re.search(
        r"(?:^|\n)ANSWER:\s*(.*)$", content, flags=re.IGNORECASE | re.DOTALL
    )
    transcript = transcript_match.group(1).strip() if transcript_match else ""
    answer = answer_match.group(1).strip() if answer_match else content
    if not answer:
        raise RuntimeError("The local model returned an empty answer")
    return transcript, oled_safe_text(answer)


def transcribe_locally(wav_file: io.BytesIO) -> str:
    wav_file.seek(0)
    with wave.open(wav_file, "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ValueError("PocketSphinx requires mono 16-bit PCM audio")
        sample_rate = audio.getframerate()
        pcm = audio.readframes(audio.getnframes())
    decoder = Decoder(samprate=sample_rate, loglevel="ERROR")
    decoder.start_utt()
    decoder.process_raw(pcm, full_utt=True)
    decoder.end_utt()
    hypothesis = decoder.hyp()
    if hypothesis is None or not hypothesis.hypstr.strip():
        raise RuntimeError("Local speech recognition could not understand the recording")
    return hypothesis.hypstr.strip()


def ask_ollama(question: str, ollama_url: str, model: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You answer questions for a 128x64 monochrome OLED. "
                    "Use plain ASCII text with no Markdown. Answer directly in "
                    "under 180 characters using short, simple sentences."
                ),
            },
            {
                "role": "user",
                "content": question,
            },
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 100},
    }
    endpoint = ollama_url.rstrip("/") + "/api/chat"
    try:
        response = requests.post(endpoint, json=payload, timeout=120)
    except requests.ConnectionError as error:
        raise RuntimeError(
            "Ollama is not running. Start Ollama, then run: "
            f"ollama pull {model}"
        ) from error
    if not response.ok:
        detail = response.text[:500]
        raise RuntimeError(f"Ollama HTTP {response.status_code}: {detail}")
    body = response.json()
    try:
        content = body["message"]["content"]
    except (KeyError, TypeError) as error:
        raise RuntimeError(f"Unexpected Ollama response: {body!r}") from error
    if not isinstance(content, str):
        raise RuntimeError("Ollama returned non-text content")
    _, answer = parse_model_output(content)
    return answer


def process_one_question(
    port: serial.Serial,
    ollama_url: str,
    model: str,
) -> None:
    header = wait_for_audio_header(port)
    raw_audio = read_exact(port, header.byte_count)
    wav_file = unsigned_u16_to_wav(raw_audio, header.sample_rate)

    send_command(port, "status", text="Transcribing...")
    transcript = transcribe_locally(wav_file)
    print(f"Question: {transcript}")
    send_command(port, "status", text="Thinking...")
    answer = ask_ollama(transcript, ollama_url, model)
    print(f"Answer: {answer}")
    send_command(port, "answer", text=answer)
    wait_for_board_done(port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default=os.getenv("EDU_PICO_PORT"))
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--ollama-url", default=os.getenv("OLLAMA_URL", DEFAULT_OLLAMA_URL)
    )
    parser.add_argument("--once", action="store_true", help="Exit after one question")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    port_name = discover_port(args.port)
    print(f"Opening EDU PICO data port {port_name}")
    print(f"Local Ollama model: {args.model}")

    with serial.Serial(port_name, args.baud, timeout=12, write_timeout=5) as port:
        port.reset_input_buffer()
        while True:
            try:
                process_one_question(port, args.ollama_url, args.model)
                if args.once:
                    return 0
            except KeyboardInterrupt:
                return 0
            except Exception as error:
                print(f"Error: {error}", file=sys.stderr)
                try:
                    send_command(port, "error", text=oled_safe_text(str(error), 80))
                except (serial.SerialException, OSError):
                    pass
                time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())

