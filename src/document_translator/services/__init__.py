"""Service-layer utilities for document translation."""

from .cache import (
    CacheClosedError,
    CacheCorruptionError,
    TranslationCache,
    TranslationCacheError,
)
from .markdown_translation import (
    MarkdownTranslationOutcome,
    MarkdownTranslationService,
    MarkdownTranslationServiceError,
    ProviderConfig,
    UnitTranslationProvider,
)
from .docx_translation import (
    DocxTranslationOutcome,
    DocxTranslationService,
    DocxTranslationServiceError,
    write_docx_comparison_report,
)
from .glossary import Glossary, GlossaryEntry, GlossaryError, load_glossary
from .pptx_translation import PptxTranslationService
from .pptx_layout import PptxLayoutOutcome, PptxLayoutService
from .xlsx_translation import XlsxTranslationOutcome, XlsxTranslationService, XlsxTranslationServiceError
from .dwg_translation import DwgTranslationOutcome, DwgTranslationService, DwgTranslationServiceError
from .pdf_translation import PdfTranslationOutcome, PdfTranslationService
from .babeldoc_pdf import BabelDocPdfTranslationService
from .translation_gateway import TranslationGateway

__all__ = [
    "CacheClosedError",
    "CacheCorruptionError",
    "TranslationCache",
    "TranslationCacheError",
    "MarkdownTranslationOutcome",
    "MarkdownTranslationService",
    "MarkdownTranslationServiceError",
    "DocxTranslationOutcome",
    "DocxTranslationService",
    "DocxTranslationServiceError",
    "write_docx_comparison_report",
    "ProviderConfig",
    "UnitTranslationProvider",
    "Glossary",
    "GlossaryEntry",
    "GlossaryError",
    "load_glossary",
    "PptxTranslationService",
    "PptxLayoutOutcome",
    "PptxLayoutService",
    "XlsxTranslationOutcome",
    "XlsxTranslationService",
    "XlsxTranslationServiceError",
    "DwgTranslationOutcome",
    "DwgTranslationService",
    "DwgTranslationServiceError",
    "PdfTranslationOutcome",
    "PdfTranslationService",
    "BabelDocPdfTranslationService",
    "TranslationGateway",
]
