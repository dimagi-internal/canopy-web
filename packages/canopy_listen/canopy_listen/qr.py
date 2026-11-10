"""A QR code for the pairing link, made here: the link holds a one-time secret, so it never
goes to a QR service. Byte mode, error correction L or M, versions 1-40, the mask with the
lowest penalty. After ISO/IEC 18004 and Project Nayuki's reference encoder (MIT).
"""
from __future__ import annotations

# Error correction codewords per block and number of blocks, by level then version (index 0 unused).
_ECC_PER_BLOCK = {
    "L": [-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26,
          28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
    "M": [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28,
          28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28],
}
_BLOCKS = {
    "L": [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16,
          17, 18, 19, 19, 20, 21, 22, 24, 25],
    "M": [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28,
          29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49],
}
_FORMAT_BITS = {"L": 1, "M": 0}


def _raw_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver: int, ecl: str) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ecl][ver] * _BLOCKS[ecl][ver]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list, divisor: list) -> list:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


class QR:
    def __init__(self, data: bytes, ecl: str = "M"):
        for ver in range(1, 41):
            count_bits = 8 if ver < 10 else 16
            if 4 + count_bits + 8 * len(data) <= _data_codewords(ver, ecl) * 8:
                break
        else:
            raise ValueError("too long for a QR code")
        self.version, self.ecl = ver, ecl
        self.size = ver * 4 + 17
        bits: list = []

        def put(value: int, n: int) -> None:
            bits.extend((value >> i) & 1 for i in reversed(range(n)))

        put(0b0100, 4)
        put(len(data), count_bits)
        for b in data:
            put(b, 8)
        capacity = _data_codewords(ver, ecl) * 8
        put(0, min(4, capacity - len(bits)))
        put(0, (-len(bits)) % 8)
        pad = 0xEC
        while len(bits) < capacity:
            put(pad, 8)
            pad ^= 0xEC ^ 0x11
        codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

        n = self.size
        self.modules = [[False] * n for _ in range(n)]
        self.function = [[False] * n for _ in range(n)]
        self._draw_function_patterns()
        self._draw_codewords(self._add_ecc(codewords))
        best = None
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format(mask)
            score = self._penalty()
            if best is None or score < best[0]:
                best = (score, mask)
            self._apply_mask(mask)  # undo: XOR twice
        self.mask = best[1]
        self._apply_mask(self.mask)
        self._draw_format(self.mask)

    # --- layout -------------------------------------------------------------------

    def _set(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self.function[y][x] = True

    def _draw_function_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        positions = self._alignment_positions()
        last = len(positions) - 1
        for i, ax in enumerate(positions):
            for j, ay in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            value = self.version << 12 | rem
            for i in range(18):
                bit = bool((value >> i) & 1)
                a, b = n - 11 + i % 3, i // 3
                self._set(a, b, bit)
                self._set(b, a, bit)

    def _alignment_positions(self) -> list:
        if self.version == 1:
            return []
        count = self.version // 7 + 2
        step = (self.version * 8 + count * 3 + 5) // (count * 4 - 4) * 2
        return [6] + sorted(self.size - 7 - i * step for i in range(count - 1))

    def _draw_format(self, mask: int) -> None:
        data = _FORMAT_BITS[self.ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        n = self.size

        def bit(i: int) -> bool:
            return bool((bits >> i) & 1)

        for i in range(6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(8):
            self._set(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, bit(i))
        self._set(8, n - 8, True)

    def _add_ecc(self, data: list) -> list:
        ver, ecl = self.version, self.ecl
        blocks_n, ecc_len = _BLOCKS[ecl][ver], _ECC_PER_BLOCK[ecl][ver]
        raw = _raw_modules(ver) // 8
        short_n = blocks_n - raw % blocks_n
        short_len = raw // blocks_n
        divisor = _rs_divisor(ecc_len)
        blocks, k = [], 0
        for i in range(blocks_n):
            dat = data[k:k + short_len - ecc_len + (0 if i < short_n else 1)]
            k += len(dat)
            ecc = _rs_remainder(dat, divisor)
            if i < short_n:
                dat = dat + [0]
            blocks.append(dat + ecc)
        out = []
        for i in range(len(blocks[0])):
            for j, block in enumerate(blocks):
                if i != short_len - ecc_len or j >= short_n:
                    out.append(block[i])
        return out

    def _draw_codewords(self, data: list) -> None:
        n, i, total = self.size, 0, len(data) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.function[y][x] and i < total:
                        self.modules[y][x] = bool((data[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1
            right -= 2

    def _apply_mask(self, mask: int) -> None:
        rule = (
            lambda x, y: (x + y) % 2 == 0,
            lambda x, y: y % 2 == 0,
            lambda x, y: x % 3 == 0,
            lambda x, y: (x + y) % 3 == 0,
            lambda x, y: (x // 3 + y // 2) % 2 == 0,
            lambda x, y: x * y % 2 + x * y % 3 == 0,
            lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
        )[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and rule(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty(self) -> int:
        """Rules 1, 2 and 4 of the standard (runs, blocks, balance): enough to avoid a bad mask."""
        n, m, score = self.size, self.modules, 0
        for lines in (m, [list(col) for col in zip(*m, strict=True)]):
            for line in lines:
                run, prev = 0, None
                for v in line:
                    if v == prev:
                        run += 1
                    else:
                        if run >= 5:
                            score += run - 2
                        run, prev = 1, v
                if run >= 5:
                    score += run - 2
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark, total = sum(sum(row) for row in m), n * n
        score += max(0, (abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
        return score

    # --- output -------------------------------------------------------------------

    def svg(self, border: int = 4) -> str:
        """Dark modules as one path; colours come from CSS (currentColor), so it suits either theme
        as long as the page puts it on a light tile."""
        parts = [f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(self.modules)
                 for x, dark in enumerate(row) if dark]
        dim = self.size + border * 2
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">'
                f'<rect width="100%" height="100%" fill="#fff"/><path d="{"".join(parts)}" fill="#000"/></svg>')


def svg(text: str, ecl: str = "M") -> str:
    return QR(text.encode(), ecl).svg()
