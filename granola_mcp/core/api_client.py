"""
Cliente HTTP para a API interna do Granola (`https://api.granola.ai`).

A API publica oficial (`public-api.granola.ai`) requer plano Business. Esta
implementacao usa a mesma API que o app desktop chama, autenticada com o
mesmo bearer token. Foi engenharia-reversada pela comunidade depois que o
Granola criptografou o cache local em mar/2026.

Rate limit: 5 req/s sustentado, burst de 25 em 5s.
"""

import gzip
import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .auth import GranolaAuthError, get_access_token


API_BASE = "https://api.granola.ai"
USER_AGENT = "GranolaMCP/2.0"


class GranolaApiError(Exception):
    """Falha em chamada a API do Granola."""

    def __init__(self, message: str, status: Optional[int] = None, body: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.body = body


class GranolaApiClient:
    """Wrapper minimalista para a API interna do Granola.

    Cacheia o token entre chamadas; refresca apenas se receber 401.
    """

    def __init__(self, base_url: str = API_BASE, timeout: int = 30):
        self.base_url = base_url
        self.timeout = timeout
        self._token: Optional[str] = None
        self._last_request_ts: float = 0.0
        self._min_interval = 0.2  # 5 req/s

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _rate_limit(self) -> None:
        delta = time.monotonic() - self._last_request_ts
        if delta < self._min_interval:
            time.sleep(self._min_interval - delta)
        self._last_request_ts = time.monotonic()

    def _token_value(self, refresh: bool = False) -> str:
        if self._token is None or refresh:
            self._token = get_access_token()
        return self._token

    def _post(self, path: str, body: Optional[dict] = None, retried: bool = False) -> Any:
        self._rate_limit()
        token = self._token_value()
        payload = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "gzip",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = ""
            try:
                body_text = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            if exc.code == 401 and not retried:
                # Token expirou: tenta refresh uma vez
                self._token_value(refresh=True)
                return self._post(path, body, retried=True)
            if exc.code == 401:
                raise GranolaApiError(
                    "Token expirado. Abra o app Granola por uns segundos para refrescar "
                    "(ele reescreve supabase.json.enc).",
                    status=401,
                    body=body_text,
                )
            if exc.code == 429:
                raise GranolaApiError(
                    "Rate limit excedido (HTTP 429). Aguarde uns segundos.",
                    status=429,
                    body=body_text,
                )
            raise GranolaApiError(
                f"HTTP {exc.code} em {path}: {body_text}",
                status=exc.code,
                body=body_text,
            )
        except urllib.error.URLError as exc:
            raise GranolaApiError(f"Falha de rede em {path}: {exc.reason}") from exc

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------
    def list_documents(self, limit: int = 100, offset: int = 0,
                       include_last_viewed_panel: bool = False) -> dict:
        """POST /v2/get-documents — pagina de documentos (mais recentes primeiro)."""
        return self._post(
            "/v2/get-documents",
            {
                "limit": limit,
                "offset": offset,
                "include_last_viewed_panel": include_last_viewed_panel,
            },
        )

    def list_all_documents(self, page_size: int = 100, max_pages: int = 50) -> list[dict]:
        """Pagina ate esgotar (ou ate max_pages)."""
        out: list[dict] = []
        for page in range(max_pages):
            resp = self.list_documents(limit=page_size, offset=page * page_size)
            docs = resp.get("docs") or []
            if not docs:
                break
            out.extend(docs)
            if len(docs) < page_size:
                break
        return out

    def get_document_transcript(self, document_id: str) -> list[dict]:
        """POST /v1/get-document-transcript — segmentos do transcript."""
        resp = self._post("/v1/get-document-transcript", {"document_id": document_id})
        # API retorna list, mas algumas variantes retornam {transcript:[...]}
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            return resp.get("transcript") or resp.get("segments") or []
        return []

    def get_document_panels(self, document_id: str) -> list[dict]:
        """POST /v1/get-document-panels — paineis (incluindo Summary AI)."""
        resp = self._post("/v1/get-document-panels", {"document_id": document_id})
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            return resp.get("panels") or []
        return []
