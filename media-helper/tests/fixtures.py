"""Real anonymous PDFs, including Standard R2 encryption; Python standard library only."""

import hashlib
import struct

PADDING = bytes.fromhex(
    "28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a"
)


def _pad(password):
    return (password + PADDING)[:32]


def _rc4(key, value):
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    i = j = 0
    result = bytearray()
    for byte in value:
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        result.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(result)


def make_pdf(pages=3, encrypted=False, corrupt_last=False, empty_password=False):
    """Uses no student names/IDs and no external PDF software."""
    file_id = hashlib.md5(b"project2-anonymous-fixture", usedforsecurity=False).digest()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    file_key = None
    owner = user = None
    if encrypted:
        owner_key = hashlib.md5(_pad(b"fixture-owner"), usedforsecurity=False).digest()[:5]
        password = b"" if empty_password else b"fixture-user"
        owner = _rc4(owner_key, _pad(password))
        file_key = hashlib.md5(
            _pad(password) + owner + struct.pack("<i", -4) + file_id,
            usedforsecurity=False,
        ).digest()[:5]
        user = _rc4(file_key, PADDING)
    kids = []
    for page in range(1, pages + 1):
        page_id = len(objects) + 1
        stream_id = page_id + 1
        kids.append(f"{page_id} 0 R")
        objects.append(
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] "
             f"/Resources << /Font << /F1 3 0 R >> >> /Contents {stream_id} 0 R >>").encode()
        )
        content = f"BT /F1 18 Tf 30 330 Td (Anonymous page {page}) Tj ET\n".encode()
        if corrupt_last and page == pages:
            content = b"BT /F1 18 Tf (unterminated\n"
        if encrypted:
            key = hashlib.md5(
                file_key + stream_id.to_bytes(3, "little") + b"\0\0",
                usedforsecurity=False,
            ).digest()[:10]
            content = _rc4(key, content)
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    objects[1] = (f"<< /Type /Pages /Count {pages} /Kids [{' '.join(kids)}] >>").encode()
    encrypt_id = None
    if encrypted:
        encrypt_id = len(objects) + 1
        objects.append(
            (f"<< /Filter /Standard /V 1 /R 2 /Length 40 /O <{owner.hex()}> "
             f"/U <{user.hex()}> /P -4 >>").encode()
        )
    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    trailer = f"<< /Size {len(offsets)} /Root 1 0 R"
    if encrypted:
        trailer += f" /Encrypt {encrypt_id} 0 R /ID [<{file_id.hex()}> <{file_id.hex()}>]"
    result.extend(f"trailer\n{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(result)
