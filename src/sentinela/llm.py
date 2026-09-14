"""Optional semantic extraction.

Three properties matter more than capability here:
  * the system runs completely without it (disabled by default, no key required);
  * identical documents never cost twice (cache keyed by hash + prompt + model + version);
  * it may only fill fields the deterministic parser left empty, and never invents values.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from sentinela.config import Configuration, Secrets
from sentinela.domain import digest

PROMPT_DIR = Path("prompts")
ALLOWED_STATUS = {"FOUND", "NOT_FOUND", "AMBIGUOUS", "CONFLICTING"}
# Fields the LLM may contribute. Eligibility inputs are deliberately excluded: education,
# workload and assignment are decided by deterministic code or left unknown.
FILLABLE = frozenset(
    {
        "edital_number",
        "publication_date",
        "registration_start",
        "registration_deadline",
        "exam_date",
        "application_fee",
        "fee_exemption_deadline",
        "payment_deadline",
        "organizing_board",
        "validity",
        "official_application_url",
    }
)
MAX_INPUT_CHARS = 24_000
# A free tier says "not now" far more often than it says "no".
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504, 529})
# 429 here means the daily quota is spent, not that the request was too fast: waiting
# does not help, but a lighter model has its own quota and answers the same questions.
QUOTA_STATUS = frozenset({429, 404})
FALLBACK_MODELS = ("gemini-3.5-flash-lite", "gemini-flash-lite-latest")


def _retry_delay(response: Any, attempt: int) -> float:
    if response is not None:
        header = response.headers.get("retry-after")
        if header:
            try:
                return max(0.0, min(float(header), 30.0))
            except ValueError:
                pass
    return min(2.0**attempt, 12.0) + random.uniform(0, 0.5)  # noqa: S311  # nosec B311


@dataclass(slots=True)
class LLMResult:
    status: str  # OK | DISABLED | UNAVAILABLE | INVALID | CACHED
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    positions: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    model: str = ""
    model_version: str = ""
    prompt_version: str = ""

    @property
    def usable(self) -> bool:
        return self.status in ("OK", "CACHED")


class LLMProvider(Protocol):
    name: str
    model: str
    model_version: str

    def complete(self, prompt: str, document: str, json_mode: bool = True) -> str: ...


class NullProvider:
    """Always available, never calls out. Keeps the pipeline uniform when the LLM is off."""

    name = "null"
    model = ""
    model_version = ""

    def complete(self, prompt: str, document: str, json_mode: bool = True) -> str:
        raise RuntimeError("LLM desabilitado")


class OpenAICompatibleProvider:
    """Works with any /chat/completions endpoint. No vendor SDK, no lock-in."""

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        model_version: str = "",
        client: httpx.Client | None = None,
        timeout: float = 90.0,
        max_attempts: int = 3,
        sleeper: Any = time.sleep,
        fallback_models: tuple[str, ...] = FALLBACK_MODELS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_version = model_version or model
        self._key = api_key
        self._client = client
        self._timeout = timeout
        self.max_attempts = max_attempts
        self.sleeper = sleeper
        self.fallback_models = fallback_models

    def complete(self, prompt: str, document: str, json_mode: bool = True) -> str:
        """Ask once, retrying only the answers that mean "not now".

        A free tier answers 503 and 429 routinely under load. Giving up on the first one
        would throw away the whole step for a condition that clears in seconds.

        `json_mode` is right for extraction, where the reply is parsed. It is wrong for
        chat, where the reply is read by a person: forcing it there delivers a raw JSON
        object into the conversation.
        """
        owned = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout)
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": document[:MAX_INPUT_CHARS]},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        candidates = [self.model] + [name for name in self.fallback_models if name != self.model]
        try:
            last: Exception | None = None
            for model in candidates:
                payload["model"] = model
                for attempt in range(1, self.max_attempts + 1):
                    try:
                        response = client.post(
                            f"{self.base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {self._key}"},
                            json=payload,
                        )
                    except httpx.HTTPError as error:
                        last = error
                        response = None
                    if response is not None:
                        if response.status_code not in TRANSIENT_STATUS:
                            response.raise_for_status()
                            body = response.json()
                            return str(body["choices"][0]["message"]["content"])
                        last = httpx.HTTPStatusError(
                            f"HTTP {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                        if response.status_code in QUOTA_STATUS:
                            # Waiting will not refill a spent quota. Change model instead.
                            break
                    if attempt == self.max_attempts:
                        break
                    self.sleeper(_retry_delay(response, attempt))
            raise last if last is not None else RuntimeError("LLM sem resposta")
        finally:
            if owned:
                client.close()


def load_prompt(version: str, directory: Path = PROMPT_DIR) -> str:
    path = directory / f"{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt {version} nao encontrado em {directory}")
    return path.read_text(encoding="utf-8")


# Google exposes Gemini through an OpenAI-compatible endpoint, so the free tier needs no
# vendor SDK and no separate code path: only a base URL and a key.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


def build_provider(config: Configuration, secrets: Secrets) -> LLMProvider:
    if not config.llm.get("enabled"):
        return NullProvider()
    api_key = secrets.llm_api_key.get_secret_value()
    model = str(config.llm.get("model") or "")
    base_url = secrets.llm_base_url or (GEMINI_BASE_URL if model.startswith("gemini") else "")
    if not (base_url and api_key and model):
        return NullProvider()
    return OpenAICompatibleProvider(
        base_url, api_key, model, str(config.llm.get("model_version") or "")
    )


def cache_key(document_hash: str, prompt_version: str, model: str, model_version: str) -> str:
    return digest([document_hash, prompt_version, model, model_version])


def parse_payload(raw: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Reject anything that is not the exact contract. A malformed reply yields nothing."""
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return {}, [], "Resposta nao e JSON valido"
    if not isinstance(body, dict):
        return {}, [], "Resposta JSON nao e um objeto"
    fields: dict[str, dict[str, Any]] = {}
    # A model is free to reply with any shape at all; every level must be type-checked.
    raw_fields = body.get("fields")
    if not isinstance(raw_fields, dict):
        raw_fields = {}
    for name, entry in raw_fields.items():
        if name not in FILLABLE or not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "NOT_FOUND").upper()
        if status not in ALLOWED_STATUS:
            continue
        value = entry.get("value")
        if status != "FOUND" or value in (None, "", []):
            continue
        try:
            confidence = float(entry.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        evidence = str(entry.get("raw_evidence") or "")[:400]
        if not evidence:
            continue  # a claim with no quoted evidence is not usable
        fields[name] = {
            "value": value,
            "status": status,
            "confidence": max(0.0, min(confidence, 0.9)),  # never outranks a table parse
            "raw_evidence": evidence,
        }
    raw_positions = body.get("positions")
    raw_positions = raw_positions if isinstance(raw_positions, list) else []
    positions = [entry for entry in raw_positions if isinstance(entry, dict)][:60]
    return fields, positions, ""


def parse_located(raw: str) -> list[dict[str, Any]]:
    """Read a locator reply. Every claim must carry a quote or it is dropped here."""
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(body, dict):
        return []
    entries = body.get("positions")
    if not isinstance(entries, list):
        return []
    found: list[dict[str, Any]] = []
    for entry in entries[:60]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        raw_fields = entry.get("fields")
        if not name or not isinstance(raw_fields, dict):
            continue
        claims: dict[str, Any] = {}
        for name_of_field, claim in raw_fields.items():
            if not isinstance(claim, dict):
                continue
            evidence = str(claim.get("raw_evidence") or "")
            if len(evidence) < 25:
                continue  # unquotable claims never reach the verifier
            claims[name_of_field] = {
                "value": claim.get("value"),
                "raw_evidence": evidence[:400],
            }
        if claims:
            found.append({"name": name, "fields": claims})
    return found


def extract_semantic(
    provider: LLMProvider, prompt_version: str, document_text: str, enabled: bool = True
) -> LLMResult:
    if not enabled or isinstance(provider, NullProvider):
        return LLMResult("DISABLED", detail="Extracao semantica desabilitada")
    try:
        prompt = load_prompt(prompt_version)
    except FileNotFoundError as error:
        return LLMResult("UNAVAILABLE", detail=str(error))
    try:
        raw = provider.complete(prompt, document_text)
    except Exception as error:  # noqa: BLE001 - provider outage must not fail the run
        return LLMResult("UNAVAILABLE", detail=type(error).__name__)
    fields, positions, problem = parse_payload(raw)
    if problem:
        return LLMResult(
            "INVALID",
            detail=problem,
            model=provider.model,
            model_version=provider.model_version,
            prompt_version=prompt_version,
        )
    return LLMResult(
        "OK",
        fields=fields,
        positions=positions,
        model=provider.model,
        model_version=provider.model_version,
        prompt_version=prompt_version,
    )


def fill_gaps(draft: Any, result: LLMResult, source_url: str) -> list[str]:
    """Apply semantic values only where the deterministic parser found nothing."""
    from sentinela.domain import Evidence
    from sentinela.parse import parse_date

    if not result.usable:
        return []
    applied: list[str] = []
    for name, entry in result.fields.items():
        if not hasattr(draft, name) or getattr(draft, name) is not None:
            continue
        value: Any = entry["value"]
        if name.endswith(("_date", "_start", "_deadline")):
            value = parse_date(str(value))
            if value is None:
                continue
        try:
            setattr(draft, name, value)
        except (ValueError, TypeError):
            continue
        draft.evidence[name] = Evidence(
            value=value,
            status="FOUND",
            source_url=source_url,
            extraction_method=f"llm:{result.prompt_version}",
            confidence=entry["confidence"],
            raw_evidence=entry["raw_evidence"],
        )
        applied.append(name)
    return applied
