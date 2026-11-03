import io
import struct
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "agent"))

from main import (
    oled_safe_text,
    parse_model_output,
    parse_audio_header,
    unsigned_u16_to_wav,
    wait_for_audio_header,
    wait_for_board_done,
)


def test_parse_audio_header():
    header = parse_audio_header(b"AUDIO 64000 16000 U16LE\n")
    assert header.byte_count == 64000
    assert header.sample_rate == 16000
    assert header.encoding == "U16LE"


def test_parse_audio_header_rejects_bad_message():
    with pytest.raises(ValueError):
        parse_audio_header(b"HELLO\n")


def test_read_exact_accepts_partial_serial_chunks():
    from main import read_exact

    class FakePort:
        chunks = iter((b"ab", b"c", b"def"))

        def read(self, _size):
            return next(self.chunks)

    assert read_exact(FakePort(), 6) == b"abcdef"


def test_wait_for_audio_header_ignores_idle_timeouts():
    class FakePort:
        messages = iter((b"", b"", b"AUDIO 64000 16000 U16LE\n"))

        def readline(self):
            return next(self.messages)

    assert wait_for_audio_header(FakePort()).byte_count == 64000


def test_wait_for_audio_header_ignores_done_event():
    class FakePort:
        messages = iter(
            (b'{"ok":false,"event":"done"}\n', b"AUDIO 64000 16000 U16LE\n")
        )

        def readline(self):
            return next(self.messages)

    assert wait_for_audio_header(FakePort()).sample_rate == 16000


def test_unsigned_microphone_samples_become_signed_wav():
    raw = struct.pack("<3H", 1000, 2000, 3000)
    wav_buffer = unsigned_u16_to_wav(raw, 16000)
    with wave.open(io.BytesIO(wav_buffer.getvalue()), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert struct.unpack("<3h", wav.readframes(3)) == (-12000, 0, 12000)


def test_oled_safe_text_is_ascii_and_bounded():
    result = oled_safe_text("Caf\u00e9 " + "word " * 100)
    assert result.isascii()
    assert len(result) <= 180
    assert result.endswith("...")


def test_wait_for_board_done_consumes_completion_event():
    class FakePort:
        messages = iter((b"", b"", b'{"event":"done"}\n'))

        def readline(self):
            return next(self.messages)

    wait_for_board_done(FakePort())


def test_parse_labeled_transcript_and_answer():
    transcript, answer = parse_model_output(
        "TRANSCRIPT: What is a robot?\nANSWER: A robot is a programmable machine."
    )
    assert transcript == "What is a robot?"
    assert answer == "A robot is a programmable machine."

