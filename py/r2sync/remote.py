"""R2 transport, remote-object normalization, and bounded parallel work."""

from __future__ import annotations

import concurrent.futures
import datetime as _datetime
import hashlib
import hmac
import math
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import ParallelInterrupted, PullError
from .local import (
    _choose_nfc_candidate,
    _ignore_matcher,
    _record_unicode_aliases,
    _safe_relative,
    _safe_relative_spelling,
)


def _mtime_ms(metadata: dict[str, str]) -> float:
    raw = metadata.get("mtime") or metadata.get("mmtime") or metadata.get("mtime-ms")
    if raw is None:
        return time.time() * 1000
    try:
        value = float(raw)
    except ValueError as error:
        raise PullError("remote mtime metadata is invalid") from error
    if not math.isfinite(value):
        raise PullError("remote mtime metadata is invalid")
    return value if abs(value) > 100_000_000_000 else value * 1000


class RemoteObject:
    def __init__(self, data: bytes, etag: str | None = None, metadata: dict[str, str] | None = None):
        self.data = data
        self.etag = etag
        self.metadata = {str(k).lower(): str(v) for k, v in (metadata or {}).items()}


class R2Client:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, timeout: float = 30):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise PullError("endpoint must be an http(s) URL")
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout

    def _request(self, method: str, key: str = "", query: dict[str, str] | None = None, body: bytes = b"", extra_headers: dict[str, str] | None = None) -> tuple[bytes, object]:
        now = _datetime.datetime.now(_datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        parsed = urllib.parse.urlsplit(self.endpoint)
        path_parts = (self.bucket, *key.split("/")) if key else (self.bucket,)
        encoded_path = "/" + "/".join(urllib.parse.quote(part, safe="~") for part in path_parts)
        query = query or {}
        canonical_query = "&".join(
            f"{urllib.parse.quote(str(name), safe='~')}={urllib.parse.quote(str(value), safe='~')}"
            for name, value in sorted(query.items())
        )
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + encoded_path, canonical_query, ""))
        host = parsed.netloc
        payload_hash = "UNSIGNED-PAYLOAD"
        headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date, **(extra_headers or {})}
        canonical_headers = "".join(f"{name}:{' '.join(value.strip().split())}\n" for name, value in sorted(headers.items()))
        signed_headers = ";".join(sorted(headers))
        canonical_request = f"{method}\n{encoded_path}\n{canonical_query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        scope = f"{date}/auto/s3/aws4_request"
        hashed_request = hashlib.sha256(canonical_request.encode()).hexdigest()
        to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashed_request}"
        date_key = hmac.new(("AWS4" + self.secret_key).encode(), date.encode(), hashlib.sha256).digest()
        region_key = hmac.new(date_key, b"auto", hashlib.sha256).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        signing_key = hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
        try:
            request = urllib.request.Request(url, data=body if body else None, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read(), response
        except urllib.error.HTTPError as error:
            raise PullError(f"R2 {method} failed ({error.code})") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise PullError(f"R2 {method} failed (network error)") from error

    def get_object(self, key: str) -> RemoteObject:
        body, response = self._request("GET", key)
        metadata = {k.lower(): v for k, v in response.headers.items() if k.lower().startswith("x-amz-meta-")}
        metadata = {k.removeprefix("x-amz-meta-"): v for k, v in metadata.items()}
        return RemoteObject(body, response.headers.get("ETag"), metadata)

    def put_object(self, key: str, data: bytes, mtime_ms: float) -> str | None:
        _, response = self._request(
            "PUT", key, body=data,
            extra_headers={"x-amz-meta-mtime": str(mtime_ms / 1000)},
        )
        return response.headers.get("ETag")

    def delete_object(self, key: str) -> None:
        self._request("DELETE", key)

    @staticmethod
    def _xml_text(parent: ET.Element, name: str) -> str | None:
        for child in parent.iter():
            if child.tag.rsplit("}", 1)[-1] == name:
                return child.text
        return None

    def list_all(self, prefix: str = "") -> list[dict]:
        """List all objects below ``prefix`` using S3 ListObjectsV2."""
        items = []
        token = None
        while True:
            query = {"list-type": "2"}
            if prefix:
                query["prefix"] = prefix
            if token:
                query["continuation-token"] = token
            body, _ = self._request("GET", "", query)
            try:
                root = ET.fromstring(body)
            except ET.ParseError as error:
                raise PullError("R2 LIST returned invalid XML") from error
            for contents in root.iter():
                if contents.tag.rsplit("}", 1)[-1] != "Contents":
                    continue
                item = {
                    "key": self._xml_text(contents, "Key"),
                    "etag": self._xml_text(contents, "ETag"),
                    "size": self._xml_text(contents, "Size"),
                    "lastModified": self._xml_text(contents, "LastModified"),
                }
                if not isinstance(item["key"], str) or not item["key"]:
                    raise PullError("R2 LIST returned an object without a key")
                try:
                    item["size"] = int(item["size"] or 0)
                except ValueError as error:
                    raise PullError("R2 LIST returned an invalid object size") from error
                items.append(item)
            truncated = self._xml_text(root, "IsTruncated") == "true"
            token = self._xml_text(root, "NextContinuationToken") if truncated else None
            if truncated and not token:
                raise PullError("R2 LIST is truncated but has no continuation token")
            if not token:
                return items


def _run_parallel(
    items: list, worker, concurrency: int, on_progress=None, *, wait_on_interrupt: bool = False,
) -> list[tuple[object, concurrent.futures.Future]]:
    """Run work and return futures in input order.

    ThreadPoolExecutor's context manager waits for every worker while unwinding.
    That makes Ctrl+C appear ineffective when a network worker is blocked.  On
    interruption, read-only callers deliberately skip that wait. Mutation
    callers set ``wait_on_interrupt`` so the process lock is not released while
    a remote write remains active.
    """
    if not items:
        return []
    worker_count = min(concurrency, len(items))
    if worker_count == 1:
        completed = []
        for index, item in enumerate(items, start=1):
            future = concurrent.futures.Future()
            try:
                future.set_result(worker(item))
            except Exception as error:
                future.set_exception(error)
            completed.append((item, future))
            if on_progress:
                on_progress(index, len(items))
        return completed

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
    future_items = {pool.submit(worker, item): (index, item) for index, item in enumerate(items)}
    completed = [None] * len(items)
    try:
        for done, future in enumerate(concurrent.futures.as_completed(future_items), start=1):
            index, item = future_items[future]
            completed[index] = (item, future)
            if on_progress:
                on_progress(done, len(items))
    except KeyboardInterrupt as error:
        for future in future_items:
            future.cancel()
        pool.shutdown(wait=wait_on_interrupt, cancel_futures=True)
        raise ParallelInterrupted() from error
    except BaseException:
        for future in future_items:
            future.cancel()
        pool.shutdown(wait=wait_on_interrupt, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    return completed
def _remote_mtime_ms(remote: RemoteObject, listed: dict) -> float:
    if remote.metadata.get("mtime") or remote.metadata.get("mmtime") or remote.metadata.get("mtime-ms"):
        return _mtime_ms(remote.metadata)
    raw = listed.get("lastModified")
    if isinstance(raw, str) and raw:
        try:
            parsed = _datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.timestamp() * 1000
        except ValueError:
            pass
    return time.time() * 1000
def _collect_remote_objects(
    listed: list[dict], decoder, remote_prefix: str, extra_patterns: list[str] | None = None,
    protected_paths: list[str] | None = None, unicode_collision_policy: str = "error",
    collision_stats: dict | None = None,
) -> tuple[dict[str, dict], list[dict], list[dict]]:
    prefix = remote_prefix.replace("\\", "/").strip("/")
    prefix = prefix + "/" if prefix else ""
    remotes = {}
    grouped = {}
    ignored = []
    conflicts = []
    _, ignored_file = _ignore_matcher(extra_patterns, protected_paths)
    for raw in listed:
        if not isinstance(raw, dict) or not isinstance(raw.get("key"), str):
            conflicts.append({"path": "<remote-list>", "reason": "R2 LIST returned an invalid object"})
            continue
        key = raw["key"]
        if prefix and not key.startswith(prefix):
            continue
        encrypted_path = key[len(prefix):] if prefix else key
        try:
            decoded_path = decoder.decrypt_path(encrypted_path) if hasattr(decoder, "decrypt_path") else encrypted_path
            decoded_spelling = _safe_relative_spelling(decoded_path)
            rel_path = unicodedata.normalize("NFC", decoded_spelling)
        except Exception as error:
            ignored.append({"key": key, "reason": f"cannot decode object path: {error}"})
            continue
        if ignored_file(rel_path):
            ignored.append({"key": key, "path": rel_path, "reason": "ignored"})
            continue
        item = dict(raw)
        item["key"] = key
        item["_decodedPath"] = decoded_spelling
        grouped.setdefault(rel_path, []).append(item)
    for rel_path, candidates in grouped.items():
        try:
            selected = _choose_nfc_candidate(
                [candidate["_decodedPath"] for candidate in candidates], rel_path,
                unicode_collision_policy, "remote",
            )
        except PullError:
            conflicts.append({"path": rel_path, "reason": "multiple remote objects decode to the same path"})
            continue
        remotes[rel_path] = candidates[selected]
        aliases = [candidate for index, candidate in enumerate(candidates) if index != selected]
        ignored.extend({
            "key": alias["key"], "path": alias["_decodedPath"],
            "reason": "Unicode alias ignored in favor of NFC",
        } for alias in aliases)
        _record_unicode_aliases(collision_stats, len(aliases))
    return remotes, ignored, conflicts
def _remote_key(decoder, remote_prefix: str, rel_path: str) -> str:
    prefix = remote_prefix.replace("\\", "/").strip("/")
    prefix = prefix + "/" if prefix else ""
    encoded = decoder.encrypt_path(rel_path) if hasattr(decoder, "encrypt_path") else _safe_relative(rel_path)
    return prefix + encoded
