"""Layered PDF text extraction with pure-Python FlateDecode fallback."""

from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import zlib
from pathlib import Path
from typing import Any


class PdfExtractionError(RuntimeError):
    """Raised when text cannot be extracted from a PDF file."""


_STREAM_RE = re.compile(rb"(<<.*?>>)\s*stream\r?\n(.*?)\r?\n?endstream", re.DOTALL)


def _decode_pdf_literal(raw: bytes) -> str:
    """Decode a PDF parenthesized literal string (without outer parens)."""
    out = bytearray()
    i = 0
    n = len(raw)
    escapes = {
        ord("n"): ord("\n"),
        ord("r"): ord("\r"),
        ord("t"): ord("\t"),
        ord("b"): ord("\b"),
        ord("f"): ord("\f"),
        ord("("): ord("("),
        ord(")"): ord(")"),
        ord("\\"): ord("\\"),
    }
    while i < n:
        ch = raw[i]
        if ch == ord("\\") and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            if nxt in (ord("\r"), ord("\n")):
                i += 2
                if nxt == ord("\r") and i < n and raw[i] == ord("\n"):
                    i += 1
                continue
            if ord("0") <= nxt <= ord("7"):
                oct_digits = [nxt]
                j = i + 2
                while j < n and len(oct_digits) < 3 and ord("0") <= raw[j] <= ord("7"):
                    oct_digits.append(raw[j])
                    j += 1
                out.append(int(bytes(oct_digits).decode("ascii"), 8) & 0xFF)
                i = j
                continue
        out.append(ch)
        i += 1
    return out.decode("latin-1", errors="replace")


def _decode_pdf_hex(raw: bytes) -> str:
    """Decode a PDF hex string (without angle brackets)."""
    cleaned = re.sub(rb"\s+", b"", raw)
    if len(cleaned) % 2 == 1:
        cleaned += b"0"
    try:
        data = bytes.fromhex(cleaned.decode("ascii"))
    except ValueError:
        return ""
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace")
    return data.decode("latin-1", errors="replace")


def _extract_text_from_content_stream(stream_bytes: bytes) -> str:
    """Parse PDF content stream operators (BT/ET, Tj, TJ, Td, TD, Tm, T*) into text."""
    text = stream_bytes.decode("latin-1", errors="replace")
    lines: list[str] = []
    current_parts: list[str] = []
    last_y: float | None = None

    def flush_line() -> None:
        if current_parts:
            line = "".join(current_parts).strip()
            if line:
                lines.append(line)
            current_parts.clear()

    # Tokenize inside BT...ET blocks or full stream
    bt_blocks = re.findall(r"BT(.*?)ET", text, re.DOTALL)
    targets = bt_blocks if bt_blocks else [text]

    token_re = re.compile(
        r"""
        \((?P<lit>(?:\\.|[^\\()])*)\)
        | <(?P<hex>[0-9A-Fa-f\s]+)>
        | \[(?P<arr>(?:\\.|[^\[\]])*)\]
        | (?P<num>[+-]?\d+(?:\.\d+)?)
        | (?P<op>T\*|TD|Td|Tm|TJ|Tj|'|")
        """,
        re.VERBOSE | re.DOTALL,
    )

    operands: list[tuple[str, str]] = []
    for block in targets:
        operands.clear()
        for match in token_re.finditer(block):
            if match.group("lit") is not None:
                operands.append(("str", _decode_pdf_literal(match.group("lit").encode("latin-1"))))
            elif match.group("hex") is not None:
                operands.append(("str", _decode_pdf_hex(match.group("hex").encode("latin-1"))))
            elif match.group("arr") is not None:
                operands.append(("arr", match.group("arr")))
            elif match.group("num") is not None:
                operands.append(("num", match.group("num")))
            elif match.group("op") is not None:
                op = match.group("op")
                if op in {"Tj", "'", '"'}:
                    if op in {"'", '"'}:
                        flush_line()
                    for kind, val in reversed(operands):
                        if kind == "str":
                            current_parts.append(val)
                            break
                elif op == "TJ":
                    for kind, val in reversed(operands):
                        if kind == "arr":
                            arr_tokens = re.finditer(
                                r"\((?P<lit>(?:\\.|[^\\()])*)\)|<(?P<hex>[0-9A-Fa-f\s]+)>|(?P<num>[+-]?\d+(?:\.\d+)?)",
                                val,
                            )
                            for tok in arr_tokens:
                                if tok.group("lit") is not None:
                                    current_parts.append(
                                        _decode_pdf_literal(tok.group("lit").encode("latin-1"))
                                    )
                                elif tok.group("hex") is not None:
                                    current_parts.append(
                                        _decode_pdf_hex(tok.group("hex").encode("latin-1"))
                                    )
                                elif (
                                    tok.group("num") is not None and float(tok.group("num")) < -120
                                ):
                                    current_parts.append(" ")
                            break
                elif op == "T*":
                    flush_line()
                elif op in {"Td", "TD"}:
                    nums = [float(v) for k, v in operands[-2:] if k == "num"]
                    if len(nums) == 2:
                        dx, dy = nums
                        if abs(dy) > 1.0:
                            flush_line()
                        elif dx > 5.0 and current_parts:
                            current_parts.append(" ")
                elif op == "Tm":
                    nums = [float(v) for k, v in operands[-6:] if k == "num"]
                    if len(nums) == 6:
                        y_coord = nums[5]
                        if last_y is not None and abs(y_coord - last_y) > 1.0:
                            flush_line()
                        elif last_y is not None and current_parts:
                            current_parts.append(" ")
                        last_y = y_coord
                operands.clear()
        flush_line()
        last_y = None

    return "\n".join(lines)


def _extract_pure_python(path: Path) -> str:
    """Pure-Python fallback PDF text extractor supporting raw and FlateDecode streams."""
    raw = path.read_bytes()
    chunks: list[str] = []

    for match in _STREAM_RE.finditer(raw):
        header = match.group(1)
        body = match.group(2)
        stream_data = body
        if b"/FlateDecode" in header:
            try:
                stream_data = zlib.decompress(body.strip(b"\r\n"))
            except zlib.error:
                try:
                    stream_data = zlib.decompress(body)
                except zlib.error:
                    continue
        extracted = _extract_text_from_content_stream(stream_data)
        if extracted.strip():
            chunks.append(extracted)

    # Also handle raw PDF content if no explicit stream blocks were matched
    if not chunks and b"BT" in raw and b"ET" in raw:
        extracted = _extract_text_from_content_stream(raw)
        if extracted.strip():
            chunks.append(extracted)

    return "\n".join(chunks)


def extract_pdf_text(path: Path) -> str:
    """Extract text from a PDF using pdfplumber, pypdf, pdftotext, or pure-Python fallback."""
    if not path.exists():
        raise PdfExtractionError(f"PDF file not found: {path}")

    # 1. Try pdfplumber
    try:
        pdfplumber_mod: Any = importlib.import_module("pdfplumber")

        with pdfplumber_mod.open(path) as pdf:
            pages = [str(page.extract_text() or "") for page in pdf.pages]
        joined = "\n".join(pages).strip()
        if joined:
            return joined
    except ImportError:
        pass
    except Exception:
        pass

    # 2. Try pypdf
    try:
        pypdf_mod: Any = importlib.import_module("pypdf")

        reader = pypdf_mod.PdfReader(str(path))
        pages = [str(page.extract_text() or "") for page in reader.pages]
        joined = "\n".join(pages).strip()
        if joined:
            return joined
    except ImportError:
        pass
    except Exception:
        pass

    # 3. Try pdftotext CLI
    pdftotext_bin = shutil.which("pdftotext")
    if pdftotext_bin:
        try:
            proc = subprocess.run(
                [pdftotext_bin, "-layout", str(path), "-"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout
        except Exception:
            pass

    # 4. Pure-Python PDF text-stream fallback
    fallback = _extract_pure_python(path).strip()
    if fallback:
        return fallback

    raise PdfExtractionError(
        f"Unable to extract text from PDF {path}. "
        "For complex or compressed font-encoded PDFs, install optional PDF dependencies: "
        "pip install 'ledgerdemain[pdf]'"
    )
