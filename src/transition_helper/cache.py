"""Workspace cache (.cache/) and HTTP helpers."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from . import __version__

USER_AGENT = f"transition-helper/{__version__}"
TIMEOUT = 60
RETRIES = 4
RETRY_DELAY = 2.0
RETRY_STATUSES = (502, 503, 504)
# autopkgtest.ubuntu.com answers 429 with a JS snippet setting this cookie
COOKIE_CHALLENGE_RE = re.compile(r"document\.cookie\s*=\s*'([A-Za-z0-9_]+)=([^;']*)")


class FetchError(Exception):
    pass


def safe_name(key: str) -> str:
    """Turn an arbitrary key into a filesystem-safe, stable file name."""
    clean = re.sub(r"[^A-Za-z0-9._+-]+", "_", key).strip("_")[:120]
    digest = hashlib.sha1(key.encode()).hexdigest()[:10]
    return f"{clean}-{digest}" if clean != key else clean


class HTTP:
    """Thin wrapper over requests so tests can substitute a fake."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def _get(self, url: str, **kwargs) -> requests.Response:
        for attempt in range(RETRIES + 1):
            r = self.session.get(url, timeout=TIMEOUT, **kwargs)
            if r.status_code == 429 and self._answer_challenge(url, r):
                r.close()
                r = self.session.get(url, timeout=TIMEOUT, **kwargs)
            if r.status_code not in RETRY_STATUSES or attempt == RETRIES:
                return r
            r.close()
            time.sleep(RETRY_DELAY * (attempt + 1))
        return r

    def _answer_challenge(self, url: str, r: requests.Response) -> bool:
        # the challenge body is tiny, so reading it whole is fine even when streaming
        m = COOKIE_CHALLENGE_RE.search(r.text[:4096])
        if not m:
            return False
        host = urlparse(url).hostname or ""
        self.session.cookies.set(m.group(1), m.group(2), domain=host, path="/")
        return True

    def get_text(self, url: str, params: dict | None = None) -> str:
        try:
            r = self._get(url, params=params)
        except requests.RequestException as e:
            raise FetchError(f"GET {url}: {e}") from e
        if r.status_code == 404:
            raise FileNotFoundError(url)
        if r.status_code >= 400:
            raise FetchError(f"GET {url}: HTTP {r.status_code}")
        return r.text

    def get_json(self, url: str, params: dict | None = None) -> Any:
        text = self.get_text(url, params)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise FetchError(f"GET {url}: invalid JSON: {e}") from e

    def head(self, url: str) -> dict[str, str]:
        """Response headers of a HEAD request (redirects followed)."""
        try:
            r = self.session.head(url, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            raise FetchError(f"HEAD {url}: {e}") from e
        if r.status_code == 404:
            raise FileNotFoundError(url)
        if r.status_code >= 400:
            raise FetchError(f"HEAD {url}: HTTP {r.status_code}")
        return {k.lower(): v for k, v in r.headers.items()}

    def get_bytes(self, url: str) -> bytes:
        try:
            r = self._get(url)
        except requests.RequestException as e:
            raise FetchError(f"GET {url}: {e}") from e
        if r.status_code == 404:
            raise FileNotFoundError(url)
        if r.status_code >= 400:
            raise FetchError(f"GET {url}: HTTP {r.status_code}")
        return r.content

    def download(self, url: str, dest: Path, gzip_output: bool = False) -> Path:
        """Stream `url` to `dest`. With gzip_output, the file on disk is always gzip
        (already-gzipped bodies are stored as-is)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with self._get(url, stream=True) as r:
                if r.status_code == 404:
                    raise FileNotFoundError(url)
                if r.status_code >= 400:
                    raise FetchError(f"GET {url}: HTTP {r.status_code}")
                chunks = r.raw.stream(64 * 1024, decode_content=False)
                first = b""
                for chunk in chunks:
                    if chunk:
                        first = chunk
                        break
                already_gz = first[:2] == b"\x1f\x8b"
                encoded = r.headers.get("Content-Encoding", "").lower()
                if encoded and encoded != "gzip":
                    raise FetchError(f"GET {url}: unsupported Content-Encoding {encoded}")
                if gzip_output and not already_gz:
                    out = gzip.open(tmp, "wb")
                else:
                    out = open(tmp, "wb")
                    if not gzip_output and already_gz and encoded == "gzip":
                        # transport-level gzip on a non-gz resource: decode
                        out.close()
                        out = _GunzipWriter(tmp)
                with out:
                    out.write(first)
                    for chunk in chunks:
                        out.write(chunk)
        except requests.RequestException as e:
            tmp.unlink(missing_ok=True)
            raise FetchError(f"GET {url}: {e}") from e
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, dest)
        return dest


class _GunzipWriter:
    def __init__(self, path: Path):
        import zlib

        self._f = open(path, "wb")
        self._d = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def write(self, data: bytes):
        self._f.write(self._d.decompress(data))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.write(self._d.flush())
        self._f.close()

    def close(self):
        self._f.close()


class Cache:
    """Keyed JSON/text cache with TTLs under <workspace>/.cache."""

    def __init__(self, root: Path, refresh: bool = False):
        self.root = root
        self.refresh = refresh
        root.mkdir(parents=True, exist_ok=True)

    def path(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def fresh(self, path: Path, ttl: float | None) -> bool:
        if self.refresh or not path.exists():
            return False
        if ttl is None:
            return True
        return (time.time() - path.stat().st_mtime) < ttl

    def get_json(self, namespace: str, key: str, ttl: float | None) -> Any | None:
        p = self.path(namespace, safe_name(key) + ".json")
        if self.fresh(p, ttl):
            try:
                return json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def put_json(self, namespace: str, key: str, value: Any) -> None:
        p = self.path(namespace, safe_name(key) + ".json")
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(value, indent=1, default=str))
        os.replace(tmp, p)

    def cached_json(self, namespace: str, key: str, ttl: float | None, producer):
        value = self.get_json(namespace, key, ttl)
        if value is None:
            value = producer()
            self.put_json(namespace, key, value)
        return value
