"""
Adaptador da API do Granola para o formato esperado pelo restante do MCP.

Mantem a interface da `GranolaParser` legada (que lia o cache JSON local),
mas internamente chama a API REST `https://api.granola.ai` via api_client.

Trade-offs:
- `get_meetings()` carrega a LISTA de documentos (campos basicos). Transcript
  e Summary nao sao trazidos aqui — sao caros (1 req por meeting).
- `get_enriched_meeting(id)` faz 3 chamadas (doc + transcript + panels) e
  retorna um dict completo no formato Meeting-compatible.

Decisao deliberada: nao manter cache local. Cada `_get_meetings()` do tools.py
faz uma listagem fresca da API — typically <1s para ~100 docs.
"""

from typing import Any, Dict, List, Optional

from .api_client import GranolaApiClient, GranolaApiError


class GranolaParseError(Exception):
    """Compatibilidade com o codigo legado que importa esse simbolo."""


class GranolaParser:
    """Fachada sobre `GranolaApiClient` no formato esperado pelo `Meeting`."""

    def __init__(self, cache_path: Optional[str] = None):
        # cache_path eh ignorado — mantido na assinatura para retro-compat.
        self._client = GranolaApiClient()
        self._docs_cache: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # Transformacao API -> formato Meeting-compatible
    # ------------------------------------------------------------------
    @staticmethod
    def _doc_to_meeting_dict(doc: Dict[str, Any]) -> Dict[str, Any]:
        """Transforma um doc da API no shape esperado pela classe Meeting.

        Mudancas principais:
        - `google_calendar_event.start/end` -> top-level `start`/`end`
          (Meeting.start_time chega via `data['start']['dateTime']`)
        - `google_calendar_event.attendees` -> top-level `attendees`
        - `has_transcript`: True se nao foi deletado e teve fim de meeting
        """
        meeting = dict(doc)  # shallow copy

        gcal = doc.get("google_calendar_event") or {}
        if isinstance(gcal, dict):
            if "start" in gcal and "start" not in meeting:
                meeting["start"] = gcal["start"]
            if "end" in gcal and "end" not in meeting:
                meeting["end"] = gcal["end"]
            if "attendees" in gcal and "attendees" not in meeting:
                meeting["attendees"] = gcal["attendees"]

        # Heuristica de has_transcript: o doc terminou e transcript nao foi deletado.
        # Nao usar `transcribe` (que indica "transcrevendo ao vivo", nao "tem armazenado").
        ended = (doc.get("meeting_end_count") or 0) > 0
        not_deleted = doc.get("transcript_deleted_at") is None
        meeting["has_transcript"] = bool(ended and not_deleted)

        return meeting

    @staticmethod
    def _segments_to_transcript_data(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Converte segmentos da API ao shape que `Transcript` espera.

        API: `{document_id, start_timestamp, end_timestamp, text, source,
              detected_speaker_name, ...}`
        Transcript precisa de campos como `text`, opcionalmente `speaker`,
        `timestamp`, `start_timestamp`, `end_timestamp`.
        """
        out: List[Dict[str, Any]] = []
        for s in segments:
            text = s.get("text") or ""
            if not text.strip():
                continue
            seg = {
                "text": text,
                "start_timestamp": s.get("start_timestamp"),
                "end_timestamp": s.get("end_timestamp"),
            }
            speaker = s.get("detected_speaker_name") or s.get("source")
            if speaker:
                # padroniza: 'microphone' -> 'MIC', 'system' -> 'SYS'
                if speaker == "microphone":
                    speaker = "MIC"
                elif speaker == "system":
                    speaker = "SYS"
                seg["speaker"] = speaker
            out.append(seg)
        return out

    @staticmethod
    def _panels_to_ai_summary_html(panels: List[Dict[str, Any]]) -> Optional[str]:
        """Extrai HTML do `original_content` dos paineis (formato AI Summary)."""
        html_chunks: List[str] = []
        for panel in panels:
            content = panel.get("original_content")
            if content and isinstance(content, str) and not content.strip().startswith("<hr>"):
                html_chunks.append(content)
        return "\n\n".join(html_chunks) if html_chunks else None

    # ------------------------------------------------------------------
    # API publica (mantida igual a versao legacy)
    # ------------------------------------------------------------------
    def load_cache(self, force_reload: bool = False) -> Dict[str, Any]:
        """Compat: retorna um dict simbolico (nao ha mais cache local)."""
        return {"state": {"documents": {}}, "source": "granola-api"}

    def get_meetings(self, debug: bool = False) -> List[Dict[str, Any]]:
        """Lista TODOS os meetings (apenas metadata). Faz paginacao automatica."""
        if self._docs_cache is None:
            try:
                raw_docs = self._client.list_all_documents()
            except GranolaApiError as exc:
                raise GranolaParseError(str(exc)) from exc
            self._docs_cache = [self._doc_to_meeting_dict(d) for d in raw_docs]
            if debug:
                print(f"DEBUG: Loaded {len(self._docs_cache)} meetings from API")
        return self._docs_cache

    def get_meeting_by_id(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        """Busca um meeting pelo ID — apenas metadata (use get_enriched_meeting para tudo)."""
        for m in self.get_meetings():
            if m.get("id") == meeting_id:
                return m
        return None

    def get_enriched_meeting(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        """Busca um meeting completo: metadata + transcript_data + ai_summary_html.

        Faz 2-3 chamadas a API. Use quando precisar do transcript ou do summary.
        """
        base = self.get_meeting_by_id(meeting_id)
        if base is None:
            return None
        enriched = dict(base)

        if enriched.get("has_transcript"):
            try:
                segments = self._client.get_document_transcript(meeting_id)
                enriched["transcript_data"] = self._segments_to_transcript_data(segments)
            except GranolaApiError:
                # Falha de transcript nao deve impedir o resto
                enriched["transcript_data"] = []

        try:
            panels = self._client.get_document_panels(meeting_id)
            summary_html = self._panels_to_ai_summary_html(panels)
            if summary_html:
                enriched["ai_summary_html"] = summary_html
        except GranolaApiError:
            pass

        return enriched

    def validate_cache_structure(self) -> bool:
        """Compat: tenta listar meetings — se nao explodir, esta ok."""
        try:
            self.get_meetings()
            return True
        except Exception:
            return False

    def get_cache_info(self) -> Dict[str, Any]:
        """Compat: substitui cache file info por status da API."""
        info = {
            "cache_path": "api://granola.ai",
            "exists": True,
            "readable": False,
            "size_bytes": 0,
            "meeting_count": 0,
            "valid_structure": False,
        }
        try:
            meetings = self.get_meetings()
            info["readable"] = True
            info["meeting_count"] = len(meetings)
            info["valid_structure"] = True
        except Exception:
            pass
        return info

    def reload(self) -> Dict[str, Any]:
        """Forca recarregar a lista de documentos."""
        self._docs_cache = None
        self.get_meetings()
        return self.load_cache()
