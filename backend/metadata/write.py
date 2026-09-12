"""Writing generation metadata into a *copy* of an image.

The original is never touched: an original image file is never modified and never
re-encoded, and an edit reaches only a temporary copy. What this module produces
is the temporary file the push uploads with the currently applicable metadata,
and it is deleted again afterwards.

**The pixels are not re-encoded.** Every writer starts the copy without source
metadata, writes the one infotext this app deliberately produced, and carries
all rendering data across byte for byte. Pillow's ``Image.save`` would be
shorter, but it re-encodes: identical pixels, every byte different, and for a
JPEG a second generation of loss.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Mapping
from pathlib import Path

#: The keyword A1111 and its descendants write, and the one CivitAI reads.
PARAMETERS_KEYWORD = "parameters"

#: EXIF UserComment, where JPEG and WebP carry the same text.
_EXIF_USER_COMMENT = 0x9286
_EXIF_IFD = 0x8769
_EXIF_ORIENTATION = 0x0112

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class MetadataError(RuntimeError):
    """The metadata could not be written into this file."""


def write_copy(
    source: Path,
    target: Path,
    parameters: str,
    *,
    keyword: str = PARAMETERS_KEYWORD,
    extra_png_text: Mapping[str, str] | None = None,
) -> None:
    """Copy ``source`` to ``target``, carrying one prepared metadata value.

    Chooses the writer by what the file actually is, not by its extension: a
    ``.png`` that is really a JPEG would otherwise produce something no reader
    can open.
    """
    data = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    try:
        if data.startswith(_PNG_SIGNATURE):
            written = write_png(data, parameters, keyword=keyword, extra_text=extra_png_text)
        elif data.startswith(b"\xff\xd8"):
            written = write_jpeg(data, parameters)
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            written = write_webp(data, parameters)
        else:
            raise MetadataError(f"{source.name}: unknown image container.")
        tmp.write_bytes(written)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


# --- PNG ---------------------------------------------------------------------


def write_png(
    data: bytes,
    parameters: str,
    *,
    keyword: str = PARAMETERS_KEYWORD,
    extra_text: Mapping[str, str] | None = None,
) -> bytes:
    """Drop source metadata, write ``parameters`` and leave IDAT untouched.

    A PNG is a signature followed by length/type/data/CRC chunks, so swapping one
    out is exact arithmetic rather than a re-encode. The new chunk goes directly
    after IHDR, which is where generators put it and where a reader looking for
    it first will find it. A1111-family writers use latin-1 ``tEXt`` where it
    fits and uncompressed UTF-8 ``iTXt`` otherwise; matching that rule preserves
    Unicode prompts without introducing a separate caller path.
    """
    texts = [(keyword, parameters)]
    texts.extend((extra_text or {}).items())
    chunks = [_text_chunk(name, value) for name, value in texts if name != keyword]
    chunks.insert(0, _text_chunk(keyword, parameters))

    out = bytearray(_PNG_SIGNATURE)
    inserted = False
    for kind, payload in _png_chunks(data):
        if kind in (b"tEXt", b"iTXt", b"zTXt", b"eXIf"):
            continue
        out += _png_chunk(kind, payload)
        if kind == b"IHDR" and not inserted:
            for text_kind, text_payload in chunks:
                out += _png_chunk(text_kind, text_payload)
            inserted = True
    if not inserted:
        raise MetadataError("PNG without an IHDR chunk.")
    return bytes(out)


def _text_chunk(keyword: str, value: str) -> tuple[bytes, bytes]:
    try:
        return b"tEXt", keyword.encode("latin-1") + b"\x00" + value.encode("latin-1")
    except UnicodeEncodeError:
        return (
            b"iTXt",
            keyword.encode("latin-1") + b"\x00\x00\x00\x00\x00" + value.encode("utf-8"),
        )


def _png_chunks(data: bytes):
    offset = len(_PNG_SIGNATURE)
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        yield kind, payload
        offset += 12 + length
        if kind == b"IEND":
            break


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


# --- EXIF (JPEG and WebP) ----------------------------------------------------


def _exif_block(existing: bytes | None, parameters: str) -> bytes:
    """An EXIF block carrying ``parameters`` as UserComment.

    Built with Pillow's Exif container - it knows the TIFF layout - but never
    handed to ``Image.save``: only the bytes come back here, and they are spliced
    into the original file.
    """
    from PIL import Image

    orientation = None
    if existing:
        for candidate in (existing, existing[6:] if existing.startswith(b"Exif\x00\x00") else b""):
            if not candidate:
                continue
            source = Image.Exif()
            try:
                source.load(candidate)
            except Exception:
                # A broken source block is not worth carrying forward; only its
                # optional Orientation value is being salvaged for the copy.
                continue
            orientation = source.get(_EXIF_ORIENTATION)
            break

    exif = Image.Exif()
    if orientation is not None:
        exif[_EXIF_ORIENTATION] = orientation
    # The Exif sub-IFD, where the tag belongs, and nowhere else.
    #
    # An IFD0 mirror was carried for a while because an early live check did not
    # read the sub-IFD - but that run also had a doubled `Exif\0\0` prefix and a
    # BYTE-typed UserComment. With both of those fixed, draft 30530622 on
    # 2026-08-21 returned its prompt from the sub-IFD alone, so the mirror was
    # never the thing that made it work. See docs/civitai-rules.md.
    comment = b"UNICODE\x00" + parameters.encode("utf-16-be")
    ifd = exif.get_ifd(_EXIF_IFD)
    ifd[_EXIF_USER_COMMENT] = comment
    exif[_EXIF_IFD] = ifd
    return _user_comment_as_undefined(exif.tobytes())


def _user_comment_as_undefined(block: bytes) -> bytes:
    """Correct Pillow's TIFF type for UserComment from BYTE to UNDEFINED.

    Both carry the same payload, which is why Pillow can read its own output,
    but EXIF defines UserComment as type 7 and strict consumers reject type 1.
    Only the two-byte type field is changed; offsets and data stay untouched.
    """
    data = bytearray(block)
    tiff = 6  # after the ``Exif\0\0`` prefix
    byte_order = bytes(data[tiff : tiff + 2])
    if byte_order not in (b"MM", b"II"):
        raise MetadataError("EXIF without a readable TIFF byte order.")
    endian = ">" if byte_order == b"MM" else "<"

    def u16(offset: int) -> int:
        return struct.unpack_from(f"{endian}H", data, offset)[0]

    def u32(offset: int) -> int:
        return struct.unpack_from(f"{endian}I", data, offset)[0]

    def patch_ifd(relative_offset: int) -> None:
        offset = tiff + relative_offset
        for index in range(u16(offset)):
            entry = offset + 2 + index * 12
            tag = u16(entry)
            if tag == _EXIF_USER_COMMENT:
                struct.pack_into(f"{endian}H", data, entry + 2, 7)
            elif tag == _EXIF_IFD:
                patch_ifd(u32(entry + 8))

    patch_ifd(u32(tiff + 4))
    return bytes(data)


def write_jpeg(data: bytes, parameters: str) -> bytes:
    """Drop source metadata, retain display segments and copy the scan verbatim."""
    out = bytearray(data[:2])  # SOI
    offset = 2
    existing: bytes | None = None
    segments: list[bytes] = []

    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            break
        marker = data[offset : offset + 2]
        if marker in (b"\xff\xd8", b"\xff\x01") or b"\xff\xd0" <= marker <= b"\xff\xd7":
            if marker != b"\xff\xd8":
                segments.append(marker)
            offset += 2
            continue
        (length,) = struct.unpack(">H", data[offset + 2 : offset + 4])
        payload = data[offset + 4 : offset + 2 + length]
        if marker == b"\xff\xe1" and payload.startswith(b"Exif\x00\x00"):
            existing = payload
        elif marker == b"\xff\xe1" and _is_xmp(payload):
            pass
        elif marker not in (b"\xff\xed", b"\xff\xfe"):
            segments.append(marker + struct.pack(">H", len(payload) + 2) + payload)
        offset += 2 + length
        if marker == b"\xff\xda":  # start of scan - the rest is entropy-coded data
            break

    rest = data[offset:]
    # Pillow's Exif container already includes the ``Exif\0\0`` APP1 prefix.
    # Adding another one produces a segment Pillow tolerates but strict readers
    # (including ExifTool and CivitAI) reject as malformed.
    app1 = _exif_block(existing, parameters)
    if len(app1) + 2 > 0xFFFF:
        raise MetadataError("The metadata is too large for a JPEG EXIF segment.")

    # APP1 goes first, where every reader looks for it.
    out += b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
    for segment in segments:
        out += segment
    out += rest
    return bytes(out)


def _is_xmp(payload: bytes) -> bool:
    return payload.startswith(
        (b"http://ns.adobe.com/xap/1.0/\x00", b"http://ns.adobe.com/xmp/extension/\x00")
    )


# --- WebP --------------------------------------------------------------------


def write_webp(data: bytes, parameters: str) -> bytes:
    """Drop source EXIF/XMP and write a fresh RIFF ``EXIF`` chunk.

    A WebP only has room for metadata in its extended form, so a plain ``VP8 ``
    or ``VP8L`` file needs a ``VP8X`` header built for it first - carrying the
    canvas size and the flag that says an EXIF chunk follows.
    """
    chunks = list(_riff_chunks(data))
    existing = next((payload for kind, payload in chunks if kind == b"EXIF"), None)
    exif = _exif_block(existing, parameters)

    kept = [(kind, payload) for kind, payload in chunks if kind not in (b"EXIF", b"XMP ")]
    has_vp8x = any(kind == b"VP8X" for kind, _ in kept)
    if not has_vp8x:
        kept.insert(0, (b"VP8X", _vp8x_header(kept)))

    out = bytearray()
    for kind, payload in kept:
        if kind == b"VP8X":
            # The fresh EXIF exists and the source XMP does not. Every other
            # feature flag (ICC, alpha, animation) is retained.
            flags = (payload[0] | 0x08) & ~0x04
            payload = bytes([flags]) + payload[1:]
        out += _riff_chunk(kind, payload)
    out += _riff_chunk(b"EXIF", exif)

    return b"RIFF" + struct.pack("<I", len(out) + 4) + b"WEBP" + bytes(out)


def _riff_chunks(data: bytes):
    offset = 12
    while offset + 8 <= len(data):
        kind = data[offset : offset + 4]
        (size,) = struct.unpack("<I", data[offset + 4 : offset + 8])
        payload = data[offset + 8 : offset + 8 + size]
        yield kind, payload
        offset += 8 + size + (size & 1)  # chunks are padded to an even length


def _riff_chunk(kind: bytes, payload: bytes) -> bytes:
    block = kind + struct.pack("<I", len(payload)) + payload
    return block + (b"\x00" if len(payload) & 1 else b"")


def _vp8x_header(chunks: list[tuple[bytes, bytes]]) -> bytes:
    """A VP8X chunk for a file that had none: flags, then canvas size minus one."""
    width = height = 0
    for kind, payload in chunks:
        if kind == b"VP8 " and len(payload) >= 10:
            width = struct.unpack("<H", payload[6:8])[0] & 0x3FFF
            height = struct.unpack("<H", payload[8:10])[0] & 0x3FFF
            break
        if kind == b"VP8L" and len(payload) >= 5:
            bits = int.from_bytes(payload[1:5], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            break
    if not width or not height:
        raise MetadataError("WebP without a readable canvas size.")
    return (
        b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
