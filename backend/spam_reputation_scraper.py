#!/usr/bin/env python3
"""
Spam Reputation Scraper
-----------------------
A conservative replacement for "append every phone number forever" scrapers.

Key properties:
- Spanish phone normalization (+34 / 0034 / separators supported).
- Per-domain/source-group reputation: 100 pages from one site still count as ONE source.
- Evidence classes: explicit_spam, suspicious, discovery, safe.
- Persistent SQLite state with first/last seen and run counts.
- Recency decay: old numbers lose confidence.
- Whitelist support.
- Conservative export threshold: only high-confidence ACTIVE numbers go to TXT.
- CSV/JSON audit exports explain WHY a number is classified.
- Retries, backoff, timeouts, User-Agent, robots.txt checks and per-domain throttling.
- No anti-bot bypassing or CAPTCHA circumvention.

This project intentionally treats source weights as policy/configuration values,
NOT as statistically calibrated probabilities.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import random
import re
import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

APP_NAME = "spam-reputation-scraper"
APP_VERSION = "2.0.0"
DEFAULT_DB = Path("spam_reputation.sqlite3")
DEFAULT_OUTPUT_DIR = Path(".")
DEFAULT_STATE_FILE = Path("spam_state.json")
DEFAULT_THRESHOLD = 70.0
DEFAULT_REVIEW_THRESHOLD = 45.0
DEFAULT_MAX_BLOCK_AGE_DAYS = 120
DEFAULT_TIMEOUT = 12.0
DEFAULT_WORKERS = 6
DEFAULT_DOMAIN_DELAY = 0.40
DEFAULT_USER_AGENT = (
    f"{APP_NAME}/{APP_VERSION} "
    "(public-interest phone reputation research; contact: local-user)"
)

PHONE_RE = re.compile(
    r"(?<!\d)(?:(?:\+|00)\s*34[\s().-]*)?"
    r"([6789](?:[\s().-]*\d){8})(?!\d)"
)

SPAM_TERMS = (
    "spam",
    "estafa",
    "fraude",
    "fraudul",
    "phishing",
    "smishing",
    "vishing",
    "publicidad agresiva",
    "televenta",
    "telemarketing",
    "acoso telefónico",
    "acoso telefonico",
    "llamada no deseada",
    "llamadas no deseadas",
    "no deseado",
    "robocall",
    "scam",
    "unwanted",
    "sospechoso",
    "suspicious",
)

SAFE_TERMS = (
    "número fiable",
    "numero fiable",
    "número seguro",
    "numero seguro",
    "número confiable",
    "numero confiable",
    "legítimo",
    "legitimo",
    "trusted",
    "safe number",
)

EVIDENCE_BONUS = {
    "explicit_spam": 25.0,
    "suspicious": 12.0,
    "discovery": 0.0,
    "safe": -25.0,  # used only for reporting; safe penalty is handled separately
}

EVIDENCE_RANK = {
    "discovery": 0,
    "suspicious": 1,
    "explicit_spam": 2,
    "safe": 3,
}


@dataclass(frozen=True)
class Source:
    id: str
    group: str
    url: str
    weight: float
    default_evidence: str = "discovery"
    parser: str = "generic"

    def __post_init__(self) -> None:
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"Invalid source weight for {self.id}: {self.weight}")
        if self.default_evidence not in EVIDENCE_BONUS:
            raise ValueError(f"Invalid evidence kind for {self.id}: {self.default_evidence}")


@dataclass(frozen=True)
class Evidence:
    phone: str
    source_id: str
    source_group: str
    source_url: str
    weight: float
    kind: str
    context: str = ""


@dataclass
class FetchResult:
    source: Source
    ok: bool
    status_code: int | None
    text: str
    error: str | None = None
    robots_blocked: bool = False


@dataclass
class PhoneScore:
    phone: str
    score: float
    status: str
    first_seen: str
    last_seen: str
    seen_runs: int
    source_groups: int
    safe_groups: int
    best_evidence: str
    max_weight: float
    age_days: int
    reasons: list[str]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0034") and len(digits) == 13:
        digits = digits[4:]
    elif digits.startswith("34") and len(digits) == 11:
        digits = digits[2:]

    if len(digits) != 9:
        return None
    if digits[0] not in "6789":
        return None
    return digits


def extract_phone_matches(text: str) -> Iterator[tuple[str, int, int]]:
    for match in PHONE_RE.finditer(text):
        phone = normalize_phone(match.group(0))
        if phone:
            yield phone, match.start(), match.end()


def classify_context(context: str, fallback: str) -> str:
    folded = context.casefold()
    if any(term.casefold() in folded for term in SAFE_TERMS):
        return "safe"
    if any(term.casefold() in folded for term in SPAM_TERMS):
        return "explicit_spam"
    return fallback


def compact_context(text: str, start: int, end: int, radius: int = 180) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return re.sub(r"\s+", " ", text[left:right]).strip()[:500]


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return " ".join(soup.stripped_strings)


def parse_generic(source: Source, html: str) -> list[Evidence]:
    text = visible_text(html)
    output: list[Evidence] = []
    for phone, start, end in extract_phone_matches(text):
        context = compact_context(text, start, end)
        kind = classify_context(context, source.default_evidence)
        output.append(
            Evidence(
                phone=phone,
                source_id=source.id,
                source_group=source.group,
                source_url=source.url,
                weight=source.weight,
                kind=kind,
                context=context,
            )
        )
    return output


def _tellows_score_from_row(row) -> int | None:
    """
    Tellows currently exposes table rows containing position, number,
    evaluations, searches and a 1..9 score. We use the last standalone
    integer in [1, 9] as a conservative parser fallback.
    """
    cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]
    if not cells:
        return None

    candidates: list[int] = []
    for cell in cells:
        if re.fullmatch(r"[1-9]", cell):
            candidates.append(int(cell))
    return candidates[-1] if candidates else None


def parse_tellows(source: Source, html: str) -> list[Evidence]:
    soup = BeautifulSoup(html, "html.parser")
    evidence: list[Evidence] = []
    phones_seen_in_tables: set[str] = set()

    for row in soup.find_all("tr"):
        row_text = " ".join(row.stripped_strings)
        matches = list(extract_phone_matches(row_text))
        if not matches:
            continue

        score = _tellows_score_from_row(row)
        if score is None:
            fallback = source.default_evidence
        elif score >= 7:
            fallback = "explicit_spam"
        elif score == 6:
            fallback = "suspicious"
        elif score <= 4:
            fallback = "safe"
        else:
            fallback = "discovery"

        for phone, start, end in matches:
            context = compact_context(row_text, start, end)
            kind = classify_context(context, fallback)
            evidence.append(
                Evidence(
                    phone=phone,
                    source_id=source.id,
                    source_group=source.group,
                    source_url=source.url,
                    weight=source.weight,
                    kind=kind,
                    context=context,
                )
            )
            phones_seen_in_tables.add(phone)

    # Also inspect recent comments / non-table text, where Tellows exposes
    # labels such as "Estafa", "Publicidad agresiva" or "Número fiable".
    text = visible_text(html)
    for phone, start, end in extract_phone_matches(text):
        context = compact_context(text, start, end)
        kind = classify_context(context, "discovery")
        if phone in phones_seen_in_tables and kind == "discovery":
            continue
        evidence.append(
            Evidence(
                phone=phone,
                source_id=source.id,
                source_group=source.group,
                source_url=source.url,
                weight=source.weight,
                kind=kind,
                context=context,
            )
        )

    return evidence


PARSERS = {
    "generic": parse_generic,
    "tellows": parse_tellows,
}


PROVINCE_PATHS = [
    "almeria", "huelva", "cadiz", "jaen", "cordoba", "malaga", "granada", "sevilla",
    "huesca", "teruel", "zaragoza", "asturias", "islas-baleares", "las-palmas",
    "santa-cruz-de-tenerife", "cantabria", "albacete", "ciudad-real", "cuenca",
    "guadalajara", "toledo", "avila", "burgos", "leon", "palencia", "salamanca",
    "segovia", "soria", "valladolid", "zamora", "barcelona", "girona", "lleida",
    "tarragona", "alicante", "castellon", "valencia", "badajoz", "caceres",
    "a-coruna", "lugo", "orense", "pontevedra", "alava", "vizcaya", "guipuzcoa",
    "la-rioja", "murcia", "madrid", "navarra",
]

SPECIAL_PREFIXES = [
    "704", "800", "803", "806", "807", "900", "901", "902",
    "903", "905", "906", "907", "908", "909",
]

# Prefix pages retained from the upstream repository. They are all grouped
# under ONE source_group ("numerospam"), so they cannot fake corroboration.
MOBILE_PREFIXES = [
    "606","608","609","616","618","619","620","626","628","629","630","636","638","639",
    "646","648","649","650","659","660","669","676","679","680","681","682","683","686",
    "689","690","696","699","717","600","603","607","610","617","627","634","637","647",
    "661","662","663","664","666","667","670","671","672","673","674","677","678","687",
    "697","711","727","605","615","625","635","645","651","652","653","654","655","656",
    "657","658","665","675","685","691","692","747","748","612","631","632","613","622",
    "623","633","712","722","624","641","642","643","693","694","695","601","604","640",
    "611","698","621","644","668","688","684","602","744",
]


def build_sources() -> list[Source]:
    sources = [
        Source(
            id="spamcalls_es",
            group="spamcalls",
            url="https://spamcalls.net/es/country-code/34",
            weight=0.78,
            default_evidence="suspicious",
        ),
        Source(
            id="tellows_stats",
            group="tellows",
            url="https://www.tellows.es/stats",
            weight=0.95,
            default_evidence="discovery",
            parser="tellows",
        ),
        Source(
            id="cleverdialer_top_24h",
            group="cleverdialer",
            url="https://www.cleverdialer.es/top-spammer-de-las-ultimas-24-horas",
            weight=0.95,
            default_evidence="explicit_spam",
        ),
        Source(
            id="detectaspam_home",
            group="detectaspam",
            url="https://detectaspam.com/",
            weight=0.82,
            default_evidence="suspicious",
        ),
        Source(
            id="telefonospam_latest",
            group="telefonospam",
            url="https://www.telefonospam.com/ultimos",
            weight=0.82,
            default_evidence="suspicious",
        ),
        Source(
            id="slickly_es",
            group="slickly",
            url="https://slick.ly/es",
            weight=0.55,
            default_evidence="discovery",
        ),
        Source(
            id="datos_last",
            group="datostelefonicos",
            url="https://datostelefonicos.com/ultimos-buscados/es?limit=200",
            weight=0.20,
            default_evidence="discovery",
        ),
        Source(
            id="datos_top",
            group="datostelefonicos",
            url="https://datostelefonicos.com/mas-buscados/es?limit=200",
            weight=0.25,
            default_evidence="discovery",
        ),
        Source(
            id="openspam_home",
            group="openspam",
            url="https://openspam.es/",
            weight=0.88,
            default_evidence="suspicious",
        ),
    ]

    base = "https://numerospam.es"
    for slug in PROVINCE_PATHS:
        sources.append(
            Source(
                id=f"numerospam_province_{slug}",
                group="numerospam",
                url=f"{base}/prefijos/es/{slug}",
                weight=0.58,
                default_evidence="suspicious",
            )
        )
    for prefix in SPECIAL_PREFIXES:
        sources.append(
            Source(
                id=f"numerospam_special_{prefix}",
                group="numerospam",
                url=f"{base}/prefijos-especiales/es/{prefix}",
                weight=0.58,
                default_evidence="suspicious",
            )
        )
    for prefix in MOBILE_PREFIXES:
        sources.append(
            Source(
                id=f"numerospam_mobile_{prefix}",
                group="numerospam",
                url=f"{base}/moviles/es/{prefix}",
                weight=0.58,
                default_evidence="suspicious",
            )
        )
    return sources


class DomainThrottle:
    def __init__(self, delay_seconds: float) -> None:
        self.delay = max(0.0, delay_seconds)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._last: dict[str, float] = defaultdict(float)

    def wait(self, url: str) -> None:
        if self.delay <= 0:
            return
        domain = urlparse(url).netloc.casefold()
        lock = self._locks[domain]
        with lock:
            elapsed = time.monotonic() - self._last[domain]
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            self._last[domain] = time.monotonic()


class RobotsCache:
    def __init__(self, session_factory, user_agent: str, timeout: float) -> None:
        self.session_factory = session_factory
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            cached = self._cache.get(origin, "__missing__")

        if cached == "__missing__":
            robots_url = f"{origin}/robots.txt"
            rp: RobotFileParser | None
            try:
                response = self.session_factory().get(
                    robots_url,
                    timeout=self.timeout,
                    headers={"User-Agent": self.user_agent},
                )
                if response.status_code == 200:
                    rp = RobotFileParser()
                    rp.set_url(robots_url)
                    rp.parse(response.text.splitlines())
                else:
                    # No usable robots.txt -> do not invent a prohibition.
                    rp = None
            except requests.RequestException:
                rp = None
            with self._lock:
                self._cache[origin] = rp
            cached = rp

        if cached is None:
            return True
        return bool(cached.can_fetch(self.user_agent, url))


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float,
        retries: int,
        user_agent: str,
        domain_delay: float,
        respect_robots: bool,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.throttle = DomainThrottle(domain_delay)
        self._local = threading.local()
        self.robots = RobotsCache(self.session, user_agent, timeout)
        self.respect_robots = respect_robots

    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "es-ES,es;q=0.9,en;q=0.5",
                    "Cache-Control": "no-cache",
                }
            )
            self._local.session = session
        return session

    def fetch(self, source: Source) -> FetchResult:
        if self.respect_robots and not self.robots.allowed(source.url):
            return FetchResult(
                source=source,
                ok=False,
                status_code=None,
                text="",
                error="Blocked by robots.txt",
                robots_blocked=True,
            )

        last_error: str | None = None
        for attempt in range(self.retries + 1):
            try:
                self.throttle.wait(source.url)
                response = self.session().get(source.url, timeout=self.timeout)

                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {response.status_code}"
                    if attempt < self.retries:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            delay = min(float(retry_after), 30.0)
                        else:
                            delay = min(0.75 * (2**attempt) + random.random() * 0.4, 8.0)
                        time.sleep(delay)
                        continue

                response.raise_for_status()
                return FetchResult(
                    source=source,
                    ok=True,
                    status_code=response.status_code,
                    text=response.text,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(min(0.75 * (2**attempt) + random.random() * 0.4, 8.0))

        return FetchResult(
            source=source,
            ok=False,
            status_code=None,
            text="",
            error=last_error or "Unknown fetch error",
        )


class ReputationDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                sources_total INTEGER NOT NULL DEFAULT 0,
                sources_ok INTEGER NOT NULL DEFAULT 0,
                sources_failed INTEGER NOT NULL DEFAULT 0,
                robots_blocked INTEGER NOT NULL DEFAULT 0,
                evidence_rows INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS source_runs (
                run_id INTEGER NOT NULL,
                source_id TEXT NOT NULL,
                source_group TEXT NOT NULL,
                url TEXT NOT NULL,
                ok INTEGER NOT NULL,
                status_code INTEGER,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                robots_blocked INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (run_id, source_id),
                FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS phones (
                phone TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                seen_runs INTEGER NOT NULL DEFAULT 0,
                score REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OBSERVE',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evidence_state (
                phone TEXT NOT NULL,
                source_group TEXT NOT NULL,
                kind TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                hits INTEGER NOT NULL DEFAULT 1,
                max_weight REAL NOT NULL,
                last_source_id TEXT NOT NULL,
                last_url TEXT NOT NULL,
                context_hash TEXT,
                context TEXT,
                PRIMARY KEY (phone, source_group, kind),
                FOREIGN KEY (phone) REFERENCES phones(phone) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_evidence_phone ON evidence_state(phone);
            CREATE INDEX IF NOT EXISTS idx_phones_status_score ON phones(status, score DESC);
            """
        )
        self.conn.commit()

    def start_run(self, source_count: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, sources_total) VALUES(?, ?)",
            (utc_iso(), source_count),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_source_run(self, run_id: int, result: FetchResult, evidence_count: int) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO source_runs(
                run_id, source_id, source_group, url, ok, status_code,
                evidence_count, error, robots_blocked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                result.source.id,
                result.source.group,
                result.source.url,
                int(result.ok),
                result.status_code,
                evidence_count,
                result.error,
                int(result.robots_blocked),
            ),
        )

    def ingest_run(self, evidence: Iterable[Evidence], run_time: str) -> int:
        # Deduplicate page-level noise while preserving genuinely conflicting
        # evidence kinds from the same independent source group.
        best: dict[tuple[str, str, str], Evidence] = {}
        for item in evidence:
            key = (item.phone, item.source_group, item.kind)
            previous = best.get(key)
            if previous is None or item.weight > previous.weight:
                best[key] = item

        seen_phones = {key[0] for key in best}

        for phone in seen_phones:
            self.conn.execute(
                """
                INSERT INTO phones(phone, first_seen, last_seen, seen_runs, updated_at)
                VALUES(?, ?, ?, 1, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    last_seen=excluded.last_seen,
                    seen_runs=phones.seen_runs + 1,
                    updated_at=excluded.updated_at
                """,
                (phone, run_time, run_time, run_time),
            )

        for item in best.values():
            context_hash = hashlib.sha256(item.context.encode("utf-8")).hexdigest()[:16] if item.context else None
            self.conn.execute(
                """
                INSERT INTO evidence_state(
                    phone, source_group, kind, first_seen, last_seen, hits,
                    max_weight, last_source_id, last_url, context_hash, context
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(phone, source_group, kind) DO UPDATE SET
                    last_seen=excluded.last_seen,
                    hits=evidence_state.hits + 1,
                    max_weight=MAX(evidence_state.max_weight, excluded.max_weight),
                    last_source_id=excluded.last_source_id,
                    last_url=excluded.last_url,
                    context_hash=excluded.context_hash,
                    context=excluded.context
                """,
                (
                    item.phone,
                    item.source_group,
                    item.kind,
                    run_time,
                    run_time,
                    item.weight,
                    item.source_id,
                    item.source_url,
                    context_hash,
                    item.context[:500] if item.context else None,
                ),
            )

        self.conn.commit()
        return len(best)

    def finish_run(self, run_id: int, evidence_rows: int) -> None:
        row = self.conn.execute(
            """
            SELECT
                SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS ok_count,
                SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN robots_blocked=1 THEN 1 ELSE 0 END) AS robots_count
            FROM source_runs WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        self.conn.execute(
            """
            UPDATE runs SET
                finished_at=?,
                sources_ok=?,
                sources_failed=?,
                robots_blocked=?,
                evidence_rows=?
            WHERE id=?
            """,
            (
                utc_iso(),
                int(row["ok_count"] or 0),
                int(row["failed_count"] or 0),
                int(row["robots_count"] or 0),
                evidence_rows,
                run_id,
            ),
        )
        self.conn.commit()

    def all_phone_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM phones ORDER BY phone").fetchall()

    def evidence_for_phone(self, phone: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM evidence_state WHERE phone=? ORDER BY source_group, kind",
            (phone,),
        ).fetchall()

    def update_score(self, phone: str, score: float, status: str) -> None:
        self.conn.execute(
            "UPDATE phones SET score=?, status=?, updated_at=? WHERE phone=?",
            (score, status, utc_iso(), phone),
        )

    def commit(self) -> None:
        self.conn.commit()

    def is_empty(self) -> bool:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM phones").fetchone()
        return int(row["n"] or 0) == 0

    def export_state(self, path: Path) -> None:
        """Export durable reputation state as deterministic, Git-friendly JSON."""
        phones = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT phone, first_seen, last_seen, seen_runs, score, status, updated_at
                FROM phones ORDER BY phone
                """
            ).fetchall()
        ]
        evidence = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT phone, source_group, kind, first_seen, last_seen, hits,
                       max_weight, last_source_id, last_url, context_hash, context
                FROM evidence_state
                ORDER BY phone, source_group, kind
                """
            ).fetchall()
        ]
        payload = {
            "schema_version": 2,
            "generated_at": utc_iso(),
            "phones": phones,
            "evidence": evidence,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    def import_state(self, path: Path) -> tuple[int, int]:
        """Import state snapshot. Intended for fresh ephemeral CI databases."""
        if not path.exists():
            return 0, 0

        payload = json.loads(path.read_text(encoding="utf-8"))
        version = int(payload.get("schema_version", 0))
        if version != 2:
            raise ValueError(f"Unsupported state schema_version={version}")

        phones = payload.get("phones", [])
        evidence = payload.get("evidence", [])

        with self.conn:
            for row in phones:
                self.conn.execute(
                    """
                    INSERT INTO phones(
                        phone, first_seen, last_seen, seen_runs, score, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(phone) DO UPDATE SET
                        first_seen=excluded.first_seen,
                        last_seen=excluded.last_seen,
                        seen_runs=excluded.seen_runs,
                        score=excluded.score,
                        status=excluded.status,
                        updated_at=excluded.updated_at
                    """,
                    (
                        row["phone"], row["first_seen"], row["last_seen"],
                        int(row.get("seen_runs", 0)), float(row.get("score", 0.0)),
                        row.get("status", "OBSERVE"),
                        row.get("updated_at", row["last_seen"]),
                    ),
                )
            for row in evidence:
                self.conn.execute(
                    """
                    INSERT INTO evidence_state(
                        phone, source_group, kind, first_seen, last_seen, hits,
                        max_weight, last_source_id, last_url, context_hash, context
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(phone, source_group, kind) DO UPDATE SET
                        first_seen=excluded.first_seen,
                        last_seen=excluded.last_seen,
                        hits=excluded.hits,
                        max_weight=excluded.max_weight,
                        last_source_id=excluded.last_source_id,
                        last_url=excluded.last_url,
                        context_hash=excluded.context_hash,
                        context=excluded.context
                    """,
                    (
                        row["phone"], row["source_group"], row["kind"],
                        row["first_seen"], row["last_seen"], int(row.get("hits", 1)),
                        float(row.get("max_weight", 0.0)),
                        row.get("last_source_id", ""),
                        row.get("last_url", ""),
                        row.get("context_hash"),
                        row.get("context"),
                    ),
                )
        return len(phones), len(evidence)

    def latest_run(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def load_whitelist(path: Path | None) -> set[str]:
    if not path or not path.exists():
        return set()
    output: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        phone = normalize_phone(line)
        if phone:
            output.add(phone)
    return output


def score_phone(
    phone_row: sqlite3.Row,
    evidence_rows: list[sqlite3.Row],
    *,
    threshold: float,
    review_threshold: float,
    max_block_age_days: int,
    whitelist: set[str],
    now: datetime | None = None,
) -> PhoneScore:
    now = now or utc_now()
    phone = str(phone_row["phone"])
    first_seen = str(phone_row["first_seen"])
    last_seen = str(phone_row["last_seen"])
    seen_runs = int(phone_row["seen_runs"])

    age_days = max(0, (now - parse_iso(last_seen)).days)

    spam_rows = [r for r in evidence_rows if r["kind"] != "safe"]
    safe_rows = [r for r in evidence_rows if r["kind"] == "safe"]

    # Only recent-ish source groups count toward current corroboration.
    active_spam_rows = [
        r for r in spam_rows
        if (now - parse_iso(str(r["last_seen"]))).days <= 365
    ]
    active_safe_rows = [
        r for r in safe_rows
        if (now - parse_iso(str(r["last_seen"]))).days <= 180
    ]

    spam_groups = {str(r["source_group"]) for r in active_spam_rows}
    safe_groups = {str(r["source_group"]) for r in active_safe_rows}
    max_weight = max((float(r["max_weight"]) for r in active_spam_rows), default=0.0)

    kinds = {str(r["kind"]) for r in active_spam_rows}
    if "explicit_spam" in kinds:
        best_evidence = "explicit_spam"
    elif "suspicious" in kinds:
        best_evidence = "suspicious"
    elif "discovery" in kinds:
        best_evidence = "discovery"
    else:
        best_evidence = "none"

    source_strength = 45.0 * max_weight
    evidence_bonus = EVIDENCE_BONUS.get(best_evidence, 0.0)
    corroboration = 12.0 * min(max(len(spam_groups) - 1, 0), 2)
    persistence = 4.0 * min(max(seen_runs - 1, 0), 3)

    if age_days <= 7:
        recency = 8.0
        stale_penalty = 0.0
    elif age_days <= 30:
        recency = 5.0
        stale_penalty = 0.0
    elif age_days <= 90:
        recency = 2.0
        stale_penalty = 10.0
    elif age_days <= 180:
        recency = 0.0
        stale_penalty = 25.0
    else:
        recency = 0.0
        stale_penalty = 40.0

    safe_penalty = 25.0 * min(len(safe_groups), 2)

    raw_score = (
        source_strength
        + evidence_bonus
        + corroboration
        + persistence
        + recency
        - safe_penalty
        - stale_penalty
    )
    score = round(max(0.0, min(100.0, raw_score)), 1)

    if phone in whitelist:
        status = "ALLOW"
    elif score >= threshold and age_days <= max_block_age_days:
        status = "BLOCK"
    elif score >= review_threshold:
        status = "REVIEW"
    else:
        status = "OBSERVE"

    reasons = [
        f"max_source_weight={max_weight:.2f} -> {source_strength:.1f}",
        f"best_evidence={best_evidence} -> {evidence_bonus:+.1f}",
        f"independent_spam_groups={len(spam_groups)} -> {corroboration:+.1f}",
        f"seen_runs={seen_runs} -> {persistence:+.1f}",
        f"age_days={age_days} -> recency {recency:+.1f}, stale {stale_penalty:-.1f}",
        f"safe_groups={len(safe_groups)} -> {safe_penalty:-.1f}",
    ]
    if phone in whitelist:
        reasons.append("whitelist -> ALLOW")

    return PhoneScore(
        phone=phone,
        score=score,
        status=status,
        first_seen=first_seen,
        last_seen=last_seen,
        seen_runs=seen_runs,
        source_groups=len(spam_groups),
        safe_groups=len(safe_groups),
        best_evidence=best_evidence,
        max_weight=max_weight,
        age_days=age_days,
        reasons=reasons,
    )


def recalculate_scores(
    db: ReputationDB,
    *,
    threshold: float,
    review_threshold: float,
    max_block_age_days: int,
    whitelist: set[str],
) -> list[PhoneScore]:
    scores: list[PhoneScore] = []
    now = utc_now()
    for phone_row in db.all_phone_rows():
        phone = str(phone_row["phone"])
        score = score_phone(
            phone_row,
            db.evidence_for_phone(phone),
            threshold=threshold,
            review_threshold=review_threshold,
            max_block_age_days=max_block_age_days,
            whitelist=whitelist,
            now=now,
        )
        db.update_score(phone, score.score, score.status)
        scores.append(score)
    db.commit()
    return scores



def read_blocklist(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        phone
        for line in path.read_text(encoding="utf-8").splitlines()
        if (phone := normalize_phone(line.strip()))
    }


def export_vcf(block_scores: list[PhoneScore], path: Path) -> None:
    """Export one VCF containing all blocked numbers as individual vCards."""
    lines: list[str] = []
    for index, score in enumerate(sorted(block_scores, key=lambda x: x.phone), start=1):
        label = f"SPAM {score.phone}"
        lines.extend(
            [
                "BEGIN:VCARD",
                "VERSION:3.0",
                f"FN:{label}",
                f"N:SPAM;{score.phone};;;",
                f"TEL;TYPE=CELL:+34{score.phone}",
                f"NOTE:Spam reputation score {score.score:.1f}/100",
                f"UID:spam-{score.phone}@spam-reputation.local",
                "END:VCARD",
            ]
        )
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def export_source_health(db: ReputationDB, path: Path) -> dict[str, int]:
    latest = db.latest_run()
    if not latest:
        path.write_text(
            "source_id,source_group,ok,status_code,evidence_count,robots_blocked,error,url\n",
            encoding="utf-8",
        )
        return {"ok": 0, "failed": 0, "robots": 0}

    rows = db.conn.execute(
        """
        SELECT source_id, source_group, ok, status_code, evidence_count,
               robots_blocked, error, url
        FROM source_runs
        WHERE run_id=?
        ORDER BY ok ASC, source_group, source_id
        """,
        (latest["id"],),
    ).fetchall()

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "source_id", "source_group", "ok", "status_code",
                "evidence_count", "robots_blocked", "error", "url"
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["source_id"], row["source_group"], row["ok"],
                    row["status_code"], row["evidence_count"],
                    row["robots_blocked"], row["error"], row["url"]
                ]
            )

    return {
        "ok": sum(1 for r in rows if int(r["ok"]) == 1),
        "failed": sum(1 for r in rows if int(r["ok"]) == 0),
        "robots": sum(1 for r in rows if int(r["robots_blocked"]) == 1),
    }


def write_auto_update_summary(
    path: Path,
    *,
    stats: dict[str, int],
    added: set[str],
    removed: set[str],
    source_health: dict[str, int],
) -> None:
    now = utc_iso()
    lines = [
        "# Automatic spam-list update",
        "",
        f"- Last run: `{now}`",
        f"- BLOCK: **{stats['block']}**",
        f"- REVIEW: **{stats['review']}**",
        f"- OBSERVE: **{stats['observe']}**",
        f"- ALLOW: **{stats['allow']}**",
        f"- New BLOCK numbers: **{len(added)}**",
        f"- Removed from BLOCK: **{len(removed)}**",
        f"- Sources OK: **{source_health['ok']}**",
        f"- Sources failed: **{source_health['failed']}**",
        f"- Sources blocked by robots.txt: **{source_health['robots']}**",
        "",
        "The public TXT contains only numbers currently classified as `BLOCK`.",
        "Numbers may be removed automatically when reputation decays or contradictory",
        "evidence reduces their score.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def export_results(
    db: ReputationDB,
    scores: list[PhoneScore],
    output_dir: Path,
) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    txt_path = output_dir / "lista_numeros_spam.txt"
    previous_blocks = read_blocklist(txt_path)

    block_scores = sorted(
        (s for s in scores if s.status == "BLOCK"),
        key=lambda x: (-x.score, x.phone),
    )
    review_scores = sorted(
        (s for s in scores if s.status == "REVIEW"),
        key=lambda x: (-x.score, x.phone),
    )
    all_scores = sorted(scores, key=lambda x: (-x.score, x.phone))

    txt_path.write_text(
        "".join(f"{s.phone}\n" for s in sorted(block_scores, key=lambda x: x.phone)),
        encoding="utf-8",
    )

    review_txt_path = output_dir / "lista_numeros_review.txt"
    review_txt_path.write_text(
        "".join(f"{s.phone}\n" for s in sorted(review_scores, key=lambda x: x.phone)),
        encoding="utf-8",
    )

    csv_path = output_dir / "spam_reputation.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "phone",
                "score",
                "status",
                "first_seen",
                "last_seen",
                "age_days",
                "seen_runs",
                "independent_spam_groups",
                "safe_groups",
                "best_evidence",
                "max_source_weight",
                "reasons",
            ]
        )
        for s in all_scores:
            writer.writerow(
                [
                    s.phone,
                    f"{s.score:.1f}",
                    s.status,
                    s.first_seen,
                    s.last_seen,
                    s.age_days,
                    s.seen_runs,
                    s.source_groups,
                    s.safe_groups,
                    s.best_evidence,
                    f"{s.max_weight:.2f}",
                    " | ".join(s.reasons),
                ]
            )

    json_path = output_dir / "spam_reputation.json"
    payload = []
    for s in block_scores + review_scores:
        ev = db.evidence_for_phone(s.phone)
        payload.append(
            {
                "phone": s.phone,
                "score": s.score,
                "status": s.status,
                "first_seen": s.first_seen,
                "last_seen": s.last_seen,
                "age_days": s.age_days,
                "seen_runs": s.seen_runs,
                "independent_spam_groups": s.source_groups,
                "safe_groups": s.safe_groups,
                "best_evidence": s.best_evidence,
                "reasons": s.reasons,
                "evidence": [
                    {
                        "source_group": row["source_group"],
                        "kind": row["kind"],
                        "first_seen": row["first_seen"],
                        "last_seen": row["last_seen"],
                        "hits": row["hits"],
                        "max_weight": row["max_weight"],
                        "last_source_id": row["last_source_id"],
                        "last_url": row["last_url"],
                    }
                    for row in ev
                ],
            }
        )
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    stats = {
        "total": len(scores),
        "block": len(block_scores),
        "review": len(review_scores),
        "observe": sum(1 for s in scores if s.status == "OBSERVE"),
        "allow": sum(1 for s in scores if s.status == "ALLOW"),
    }
    current_blocks = {s.phone for s in block_scores}
    added = current_blocks - previous_blocks
    removed = previous_blocks - current_blocks

    changes = {
        "generated_at": utc_iso(),
        "added_count": len(added),
        "removed_count": len(removed),
        "added": sorted(added),
        "removed": sorted(removed),
    }
    (output_dir / "spam_changes.json").write_text(
        json.dumps(changes, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    stats["generated_at"] = utc_iso()
    stats["added_block"] = len(added)
    stats["removed_block"] = len(removed)
    (output_dir / "spam_stats.json").write_text(
        json.dumps(stats, indent=2) + "\n",
        encoding="utf-8",
    )

    export_vcf(block_scores, output_dir / "lista_numeros_spam.vcf")

    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    mobile_manifest = {
        "schema_version": 1,
        "generated_at": utc_iso(),
        "block_count": len(block_scores),
        "review_count": len(review_scores),
        "files": {
            "block": {
                "name": "lista_numeros_spam.txt",
                "sha256": _sha256(output_dir / "lista_numeros_spam.txt"),
            },
            "review": {
                "name": "lista_numeros_review.txt",
                "sha256": _sha256(output_dir / "lista_numeros_review.txt"),
            },
        },
    }
    (output_dir / "mobile_manifest.json").write_text(
        json.dumps(mobile_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    source_health = export_source_health(db, output_dir / "source_health.csv")
    write_auto_update_summary(
        output_dir / "AUTO_UPDATE.md",
        stats=stats,
        added=added,
        removed=removed,
        source_health=source_health,
    )
    return stats


def scrape_sources(
    db: ReputationDB,
    sources: list[Source],
    *,
    workers: int,
    timeout: float,
    retries: int,
    user_agent: str,
    domain_delay: float,
    respect_robots: bool,
) -> tuple[int, int]:
    client = HttpClient(
        timeout=timeout,
        retries=retries,
        user_agent=user_agent,
        domain_delay=domain_delay,
        respect_robots=respect_robots,
    )
    run_id = db.start_run(len(sources))
    all_evidence: list[Evidence] = []

    logging.info("Scraping %d source pages...", len(sources))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(client.fetch, source): source for source in sources}
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # defensive boundary
                result = FetchResult(
                    source=source,
                    ok=False,
                    status_code=None,
                    text="",
                    error=f"Unhandled worker error: {type(exc).__name__}: {exc}",
                )

            evidence_count = 0
            if result.ok:
                parser = PARSERS[source.parser]
                try:
                    parsed = parser(source, result.text)
                    all_evidence.extend(parsed)
                    evidence_count = len(parsed)
                    logging.info(
                        "[OK] %-28s %4d evidence items",
                        source.id,
                        evidence_count,
                    )
                except Exception as exc:
                    result.ok = False
                    result.error = f"Parse error: {type(exc).__name__}: {exc}"
                    logging.warning("[PARSE FAIL] %s: %s", source.id, result.error)
            else:
                logging.warning("[FETCH FAIL] %s: %s", source.id, result.error)

            db.record_source_run(run_id, result, evidence_count)

    run_time = utc_iso()
    evidence_rows = db.ingest_run(all_evidence, run_time)
    db.finish_run(run_id, evidence_rows)
    logging.info(
        "Run %d: %d raw evidence items -> %d deduplicated state updates",
        run_id,
        len(all_evidence),
        evidence_rows,
    )
    return run_id, evidence_rows


def report_phone(db: ReputationDB, phone_raw: str) -> int:
    phone = normalize_phone(phone_raw)
    if not phone:
        print("Invalid Spanish 9-digit phone number.")
        return 2

    row = db.conn.execute("SELECT * FROM phones WHERE phone=?", (phone,)).fetchone()
    if not row:
        print(f"{phone}: no evidence stored.")
        return 1

    print(f"{phone}: score={row['score']:.1f} status={row['status']}")
    print(f"first_seen={row['first_seen']} last_seen={row['last_seen']} seen_runs={row['seen_runs']}")
    for ev in db.evidence_for_phone(phone):
        print(
            f"- {ev['source_group']}: kind={ev['kind']} weight={ev['max_weight']:.2f} "
            f"hits={ev['hits']} last={ev['last_seen']} source={ev['last_source_id']}"
        )
        print(f"  {ev['last_url']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Conservative Spanish phone-spam reputation scraper"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["run", "scrape", "export", "report"],
        default="run",
        help="run=scrape+score+export (default)",
    )
    parser.add_argument("phone", nargs="?", help="Phone for 'report'")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_FILE,
                        help="Git-friendly durable state snapshot")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--whitelist", type=Path, default=Path("whitelist.txt"))
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--review-threshold", type=float, default=DEFAULT_REVIEW_THRESHOLD)
    parser.add_argument("--max-block-age-days", type=int, default=DEFAULT_MAX_BLOCK_AGE_DAYS)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--domain-delay", type=float, default=DEFAULT_DOMAIN_DELAY)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Disable robots.txt checks. Use only if you have permission.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    db = ReputationDB(args.db)
    try:
        if db.is_empty() and args.state.exists():
            imported_phones, imported_evidence = db.import_state(args.state)
            logging.info(
                "Imported durable state: %d phones, %d evidence rows",
                imported_phones,
                imported_evidence,
            )

        if args.command == "report":
            if not args.phone:
                raise SystemExit("report requires a phone number")
            return report_phone(db, args.phone)

        whitelist = load_whitelist(args.whitelist)

        if args.command in ("run", "scrape"):
            sources = build_sources()
            scrape_sources(
                db,
                sources,
                workers=args.workers,
                timeout=args.timeout,
                retries=args.retries,
                user_agent=args.user_agent,
                domain_delay=args.domain_delay,
                respect_robots=not args.ignore_robots,
            )

        scores = recalculate_scores(
            db,
            threshold=args.threshold,
            review_threshold=args.review_threshold,
            max_block_age_days=args.max_block_age_days,
            whitelist=whitelist,
        )

        # State is saved for scrape/run/export so ephemeral CI runners retain history.
        db.export_state(args.state)

        if args.command in ("run", "export"):
            stats = export_results(db, scores, args.output_dir)
            latest = db.latest_run()
            print(
                f"Export complete: BLOCK={stats['block']} REVIEW={stats['review']} "
                f"OBSERVE={stats['observe']} ALLOW={stats['allow']} TOTAL={stats['total']}"
            )
            if latest:
                print(
                    f"Latest run #{latest['id']}: OK={latest['sources_ok']} "
                    f"FAILED={latest['sources_failed']} ROBOTS={latest['robots_blocked']} "
                    f"EVIDENCE={latest['evidence_rows']}"
                )

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
