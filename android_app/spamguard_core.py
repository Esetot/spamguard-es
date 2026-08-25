from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

DEFAULT_RAW_BASE = "https://raw.githubusercontent.com/USUARIO/REPOSITORIO/main/data"
USER_AGENT = "SpamGuardES/0.1 Android"
PHONE_RE = re.compile(r"\D+")


def normalize_phone(raw: str) -> str | None:
    digits = PHONE_RE.sub("", raw or "")
    if digits.startswith("0034") and len(digits) == 13:
        digits = digits[4:]
    elif digits.startswith("34") and len(digits) == 11:
        digits = digits[2:]
    if len(digits) != 9 or digits[0] not in "6789":
        return None
    return digits


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_number_file(data: bytes) -> set[str]:
    text = data.decode("utf-8-sig")
    folded = text.casefold()
    if "<html" in folded or "<!doctype" in folded:
        raise ValueError("La URL devolvió HTML, no una lista de teléfonos.")
    numbers: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        phone = normalize_phone(line)
        if not phone:
            raise ValueError(f"Línea de teléfono inválida: {line[:40]!r}")
        numbers.add(phone)
    return numbers


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


@dataclass
class SyncResult:
    ok: bool
    message: str
    block_count: int = 0
    review_count: int = 0
    generated_at: str | None = None


class SpamGuardStore:
    def __init__(self, files_dir: Path):
        self.files_dir = files_dir
        self.block_path = files_dir / "blocklist.txt"
        self.review_path = files_dir / "reviewlist.txt"
        self.meta_path = files_dir / "sync_meta.json"
        self.config_path = files_dir / "repo_config.json"

    def load_raw_base(self) -> str:
        if self.config_path.exists():
            try:
                value = json.loads(self.config_path.read_text(encoding="utf-8")).get("raw_base", "")
                if value:
                    return value.rstrip("/")
            except Exception:
                pass
        bundled = Path(__file__).with_name("repo_config.json")
        if bundled.exists():
            try:
                value = json.loads(bundled.read_text(encoding="utf-8")).get("raw_base", "")
                if value:
                    return value.rstrip("/")
            except Exception:
                pass
        return DEFAULT_RAW_BASE

    def save_raw_base(self, raw_base: str) -> None:
        raw_base = raw_base.strip().rstrip("/")
        if not raw_base.startswith("https://"):
            raise ValueError("La URL debe comenzar por https://")
        atomic_write(self.config_path, (json.dumps({"raw_base": raw_base}, indent=2) + "\n").encode("utf-8"))

    def stats(self) -> dict:
        result = {"block_count": self._count(self.block_path), "review_count": self._count(self.review_path), "last_sync": None, "generated_at": None}
        if self.meta_path.exists():
            try:
                result.update(json.loads(self.meta_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return result

    def lookup(self, raw_phone: str) -> tuple[str, str]:
        phone = normalize_phone(raw_phone)
        if not phone:
            return "INVALID", "Introduce un número español válido de 9 cifras."
        if phone in self._load_set(self.block_path):
            return "BLOCK", f"{phone}: spam de alta confianza."
        if phone in self._load_set(self.review_path):
            return "REVIEW", f"{phone}: sospechoso, en revisión."
        return "CLEAR", f"{phone}: no figura en la base local."

    @staticmethod
    def _count(path: Path) -> int:
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    @staticmethod
    def _load_set(path: Path) -> set[str]:
        if not path.exists():
            return set()
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


class GitHubSync:
    def __init__(self, store: SpamGuardStore, timeout: float = 15.0):
        self.store = store
        self.timeout = timeout

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain;q=0.9,*/*;q=0.1", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            if getattr(response, "status", 200) != 200:
                raise urllib.error.HTTPError(url, response.status, "HTTP error", response.headers, None)
            return response.read()

    def sync(self, raw_base: str | None = None) -> SyncResult:
        base = (raw_base or self.store.load_raw_base()).rstrip("/")
        if "USUARIO/REPOSITORIO" in base:
            return SyncResult(False, "Configura primero la URL RAW de tu repositorio GitHub.")
        try:
            manifest_data = self._get(base + "/mobile_manifest.json")
            manifest = json.loads(manifest_data.decode("utf-8"))
            if int(manifest.get("schema_version", 0)) != 1:
                raise ValueError("Versión de mobile_manifest no compatible.")
            block_info = manifest["files"]["block"]
            review_info = manifest["files"]["review"]
            block_data = self._get(base + "/" + block_info["name"])
            review_data = self._get(base + "/" + review_info["name"])
            if sha256_bytes(block_data) != block_info["sha256"]:
                raise ValueError("SHA-256 de la lista BLOCK no coincide.")
            if sha256_bytes(review_data) != review_info["sha256"]:
                raise ValueError("SHA-256 de la lista REVIEW no coincide.")
            block_numbers = parse_number_file(block_data)
            review_numbers = parse_number_file(review_data)
            if block_numbers & review_numbers:
                raise ValueError("Un número aparece simultáneamente en BLOCK y REVIEW.")
            atomic_write(self.store.block_path, "".join(f"{n}\n" for n in sorted(block_numbers)).encode("utf-8"))
            atomic_write(self.store.review_path, "".join(f"{n}\n" for n in sorted(review_numbers)).encode("utf-8"))
            meta = {"last_sync": utc_iso(), "generated_at": manifest.get("generated_at"), "block_count": len(block_numbers), "review_count": len(review_numbers)}
            atomic_write(self.store.meta_path, (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            return SyncResult(True, "Base actualizada correctamente.", len(block_numbers), len(review_numbers), manifest.get("generated_at"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            return SyncResult(False, f"Error de red: {exc}")
        except Exception as exc:
            return SyncResult(False, f"Actualización rechazada: {exc}")


def sync_async(syncer: GitHubSync, callback: Callable[[SyncResult], None], raw_base: str | None = None) -> threading.Thread:
    def worker() -> None:
        callback(syncer.sync(raw_base))
    thread = threading.Thread(target=worker, name="spamguard-sync", daemon=True)
    thread.start()
    return thread
