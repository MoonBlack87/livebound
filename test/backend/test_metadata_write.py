"""Writing metadata into a copy, without re-encoding the picture.

The claim this file has to hold up is stronger than "the image still looks the
same": the compressed image data has to come out byte for byte identical. A
Pillow round trip would pass a pixel comparison and fail this one - and for a
JPEG it would also cost a second generation of loss.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from backend.metadata import png_io, write

TEXT = "a cat, masterpiece\nNegative prompt: blurry\nSteps: 30, Seed: 42, Model: X"


@pytest.fixture
def picture() -> Image.Image:
    image = Image.new("RGB", (64, 48))
    for x in range(64):
        for y in range(48):
            image.putpixel((x, y), (x * 4 % 256, y * 5 % 256, 30))
    return image


def _png_keyword(payload: bytes) -> str:
    """The keyword of a tEXt/iTXt chunk - only this test needs to read one."""
    keyword, _, _ = payload.partition(b"\x00")
    return keyword.decode("latin-1", "replace")


def _idat(data: bytes) -> bytes:
    """Every IDAT payload concatenated - the compressed picture itself."""
    out = bytearray()
    offset = 8
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        if kind == b"IDAT":
            out += data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IEND":
            break
    return bytes(out)


def _scan(data: bytes) -> bytes:
    """Everything after the start-of-scan marker in a JPEG."""
    return data[data.index(b"\xff\xda") :]


def _jpeg_segments(data: bytes) -> list[tuple[bytes, bytes]]:
    segments: list[tuple[bytes, bytes]] = []
    offset = 2
    while offset + 4 <= len(data) and data[offset] == 0xFF:
        marker = data[offset : offset + 2]
        length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        payload = data[offset + 4 : offset + 2 + length]
        segments.append((marker, payload))
        offset += 2 + length
        if marker == b"\xff\xda":
            break
    return segments


def _jpeg_segment(marker: bytes, payload: bytes) -> bytes:
    return marker + struct.pack(">H", len(payload) + 2) + payload


@pytest.mark.parametrize(
    "name,save",
    [
        ("plain.png", lambda image, path: image.save(path, "PNG")),
        ("photo.jpg", lambda image, path: image.save(path, "JPEG", quality=90)),
        ("lossy.webp", lambda image, path: image.save(path, "WEBP", quality=90)),
        ("lossless.webp", lambda image, path: image.save(path, "WEBP", lossless=True)),
    ],
)
def test_the_text_arrives_and_the_original_is_untouched(picture, tmp_path, name, save):
    source: Path = tmp_path / name
    save(picture, source)
    before = source.read_bytes()

    target = tmp_path / f"out-{name}"
    write.write_copy(source, target, TEXT)

    assert png_io.read(target).infotext == TEXT
    assert source.read_bytes() == before, "the original is never written"
    with Image.open(source) as a, Image.open(target) as b:
        assert a.convert("RGB").tobytes() == b.convert("RGB").tobytes()


@pytest.mark.parametrize("keyword", ["parameters", "prompt"])
def test_a_png_keeps_its_compressed_data_byte_for_byte(picture, tmp_path, keyword):
    source = tmp_path / "a.png"
    picture.save(source, "PNG")
    target = tmp_path / "b.png"

    write.write_copy(source, target, TEXT, keyword=keyword)

    assert _idat(target.read_bytes()) == _idat(source.read_bytes())


def test_a_jpeg_keeps_its_scan_byte_for_byte(picture, tmp_path):
    """Re-encoding would be a second generation of loss on every edit."""
    source = tmp_path / "a.jpg"
    picture.save(source, "JPEG", quality=90)
    target = tmp_path / "b.jpg"

    write.write_copy(source, target, TEXT)

    assert _scan(target.read_bytes()) == _scan(source.read_bytes())
    assert target.read_bytes()[6:12] == b"Exif\x00\x00"
    assert target.read_bytes()[12:18] != b"Exif\x00\x00"
    # UNDEFINED (7), not Pillow's default BYTE (1) - a strict reader rejects
    # BYTE, and CivitAI ignored the file until this was fixed. Once, in the Exif
    # sub-IFD: no IFD0 mirror, measured unnecessary on 2026-08-21.
    assert target.read_bytes().count(b"\x92\x86\x00\x07") == 1


def test_an_existing_parameters_chunk_is_replaced_not_doubled(picture, tmp_path):
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", "the old text")
    source = tmp_path / "a.png"
    picture.save(source, "PNG", pnginfo=info)
    target = tmp_path / "b.png"

    write.write_copy(source, target, TEXT)

    assert png_io.read(target).infotext == TEXT
    assert target.read_bytes().count(b"parameters") == 1


def test_a_native_parser_can_keep_its_own_png_keyword(picture, tmp_path):
    source = tmp_path / "a.png"
    picture.save(source, "PNG")
    target = tmp_path / "b.png"
    graph = '{"1":{"class_type":"KSampler","inputs":{}}}'

    before = source.read_bytes()
    write.write_copy(source, target, graph, keyword="prompt")

    chunks = dict(write._png_chunks(target.read_bytes()))
    assert chunks[b"tEXt"].startswith(b"prompt\x00")
    assert b"parameters\x00" not in target.read_bytes()
    assert source.read_bytes() == before
    assert _idat(target.read_bytes()) == _idat(before)


def test_a_png_can_carry_a_workflow_alongside_its_prompt(picture, tmp_path):
    source = tmp_path / "a.png"
    picture.save(source, "PNG")
    target = tmp_path / "b.png"

    write.write_copy(
        source,
        target,
        '{"1": {"class_type": "KSampler", "inputs": {}}}',
        keyword="prompt",
        extra_png_text={"workflow": '{"nodes": []}'},
    )

    chunks = {
        payload.partition(b"\x00")[0]: payload
        for kind, payload in write._png_chunks(target.read_bytes())
        if kind == b"tEXt"
    }
    assert chunks[b"prompt"].startswith(b"prompt\x00")
    assert chunks[b"workflow"] == b'workflow\x00{"nodes": []}'


def test_copy_keeps_rendering_blocks_and_only_writes_our_metadata(picture, tmp_path):
    icc = b"test-rendering-profile"
    exif = Image.Exif()
    exif[write._EXIF_ORIENTATION] = 6
    exif[0x013B] = "foreign artist"
    exif_ifd = exif.get_ifd(write._EXIF_IFD)
    exif_ifd[write._EXIF_USER_COMMENT] = b"ASCII\x00\x00\x00foreign"
    exif[write._EXIF_IFD] = exif_ifd

    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", "old")
    info.add_text("workflow", '{"nodes": []}')
    info.add_itxt("XML:com.adobe.xmp", "<xmp>foreign</xmp>")
    png_source = tmp_path / "source.png"
    picture.save(png_source, "PNG", pnginfo=info, icc_profile=icc, exif=exif.tobytes())

    jpeg_source = tmp_path / "source.jpg"
    picture.save(jpeg_source, "JPEG", quality=90, icc_profile=icc, exif=exif.tobytes())
    jpeg = jpeg_source.read_bytes()
    foreign = b"".join(
        (
            _jpeg_segment(b"\xff\xe1", b"http://ns.adobe.com/xap/1.0/\x00<xmp/>"),
            _jpeg_segment(b"\xff\xed", b"Photoshop 3.0\x00foreign"),
            _jpeg_segment(b"\xff\xfe", b"foreign comment"),
        )
    )
    jpeg_source.write_bytes(jpeg[:2] + foreign + jpeg[2:])

    webp_source = tmp_path / "source.webp"
    rgba = picture.convert("RGBA")
    rgba.putalpha(Image.new("L", rgba.size, 128))
    rgba.save(
        webp_source,
        "WEBP",
        lossless=True,
        icc_profile=icc,
        exif=exif.tobytes(),
        xmp=b"<xmp>foreign</xmp>",
    )

    for source in (png_source, jpeg_source, webp_source):
        target = tmp_path / f"copy-{source.name}"
        write.write_copy(source, target, TEXT)
        assert png_io.read(target).infotext == TEXT, source.suffix

        source_data = source.read_bytes()
        target_data = target.read_bytes()
        if source.suffix == ".png":
            source_chunks = list(write._png_chunks(source_data))
            target_chunks = list(write._png_chunks(target_data))
            assert any(kind == b"iCCP" for kind, _ in source_chunks)
            assert [
                (kind, payload)
                for kind, payload in target_chunks
                if kind in (b"tEXt", b"iTXt", b"zTXt", b"eXIf")
            ] == [
                next(
                    (kind, payload)
                    for kind, payload in target_chunks
                    if kind in (b"tEXt", b"iTXt")
                    and _png_keyword(payload) == write.PARAMETERS_KEYWORD
                )
            ]
            for kind in (b"iCCP", b"sRGB", b"gAMA", b"cHRM", b"PLTE", b"tRNS", b"pHYs"):
                assert [p for k, p in target_chunks if k == kind] == [
                    p for k, p in source_chunks if k == kind
                ]
            assert _idat(target_data) == _idat(source_data)
        elif source.suffix == ".jpg":
            source_segments = _jpeg_segments(source_data)
            target_segments = _jpeg_segments(target_data)
            assert any(marker == b"\xff\xe2" for marker, _ in source_segments)
            assert not any(
                marker in (b"\xff\xed", b"\xff\xfe")
                or (marker == b"\xff\xe1" and write._is_xmp(payload))
                for marker, payload in target_segments
            )
            assert sum(
                marker == b"\xff\xe1" and payload.startswith(b"Exif\x00\x00")
                for marker, payload in target_segments
            ) == 1
            for marker in (b"\xff\xe0", b"\xff\xe2", b"\xff\xee"):
                assert [p for m, p in target_segments if m == marker] == [
                    p for m, p in source_segments if m == marker
                ]
            with Image.open(target) as result:
                written_exif = result.getexif()
                assert written_exif[write._EXIF_ORIENTATION] == 6
                assert 0x013B not in written_exif
            assert _scan(target_data) == _scan(source_data)
        else:
            source_chunks = list(write._riff_chunks(source_data))
            target_chunks = list(write._riff_chunks(target_data))
            assert any(kind == b"ICCP" for kind, _ in source_chunks)
            assert not any(kind == b"XMP " for kind, _ in target_chunks)
            assert sum(kind == b"EXIF" for kind, _ in target_chunks) == 1
            for kind in (b"ICCP", b"ALPH", b"VP8 ", b"VP8L", b"ANIM", b"ANMF"):
                assert [p for k, p in target_chunks if k == kind] == [
                    p for k, p in source_chunks if k == kind
                ]



def test_a_png_uses_itxt_for_unicode_and_reads_it_back_identically(picture, tmp_path):
    source = tmp_path / "a.png"
    picture.save(source, "PNG")
    target = tmp_path / "b.png"
    text = "a cat — a cat’s eye 🐱 猫"

    write.write_copy(source, target, text)

    assert png_io.read(target).infotext == text
    assert b"iTXtparameters\x00\x00\x00\x00\x00" in target.read_bytes()
    assert _idat(target.read_bytes()) == _idat(source.read_bytes())


def test_a_png_keeps_latin1_metadata_in_text(picture, tmp_path):
    source = tmp_path / "a.png"
    picture.save(source, "PNG")
    target = tmp_path / "b.png"
    text = "schoen überbelichtet"

    write.write_copy(source, target, text)

    assert png_io.read(target).infotext == text
    assert b"tEXtparameters\x00" in target.read_bytes()


def test_an_existing_exif_comment_is_replaced(picture, tmp_path):
    exif = Image.Exif()
    ifd = exif.get_ifd(0x8769)
    ifd[0x9286] = b"UNICODE\x00" + "the old text".encode("utf-16-be")
    exif[0x8769] = ifd
    source = tmp_path / "a.jpg"
    picture.save(source, "JPEG", exif=exif.tobytes())
    target = tmp_path / "b.jpg"

    write.write_copy(source, target, TEXT)

    assert png_io.read(target).infotext == TEXT

    with Image.open(target) as result:
        exif = result.getexif()
        assert exif.get_ifd(write._EXIF_IFD)[write._EXIF_USER_COMMENT] == (
            b"UNICODE\x00" + TEXT.encode("utf-16-be")
        )
        assert write._EXIF_USER_COMMENT not in exif, "IFD0 carries no copy"


def test_the_decoder_decides_the_type_not_the_file_name(picture, tmp_path):
    """A renamed download must not be announced to CivitAI as what it is not.

    All four of the maintainer's test files are PNGs carrying a `.jpeg` name.
    This type is stored, served, and sent with the upload - and a pre-signed PUT
    keeps whatever Content-Type it is handed, so the wrong one would survive on
    CivitAI's side.
    """
    lying = tmp_path / "actually-a-png.jpeg"
    picture.save(lying, format="PNG")
    assert png_io.read(lying).content_type == "image/png"

    honest = tmp_path / "really-a-jpeg.jpg"
    picture.save(honest, format="JPEG")
    assert png_io.read(honest).content_type == "image/jpeg"
