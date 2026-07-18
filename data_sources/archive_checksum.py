"""Binance 官方归档 `.CHECKSUM` 文件解析与 SHA-256 校验。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from data_sources.base import DataSourceError


def parse_checksum_file(content: str) -> str:
    """从官方 `.CHECKSUM` 文本中解析出期望的 SHA-256 十六进制摘要。

    官方格式为 ``<sha256>  <filename>``，这里只取首个非空行的第一段。
    """
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        digest = stripped.split()[0].strip().lower()
        if len(digest) != 64 or not _is_hex(digest):
            raise DataSourceError("官方 CHECKSUM 内容不是合法的 SHA-256 摘要。")
        return digest
    raise DataSourceError("官方 CHECKSUM 文件为空。")


def sha256_hexdigest(data: bytes) -> str:
    return sha256(data).hexdigest()


def sha256_file(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_official_checksum(data: bytes, checksum_content: str) -> str:
    """校验 ZIP 字节与官方摘要一致，返回本地计算出的摘要。"""
    expected = parse_checksum_file(checksum_content)
    actual = sha256_hexdigest(data)
    if actual != expected:
        raise DataSourceError(
            f"官方 checksum 不匹配：期望 {expected[:12]}…，实际 {actual[:12]}…。"
        )
    return actual


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
