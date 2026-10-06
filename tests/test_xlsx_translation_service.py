from __future__ import annotations

from dataclasses import dataclass

from document_translator.core import TranslationResult, TranslationUnit, sha256_text
from document_translator.services.xlsx_translation import XlsxTranslationService

from test_xlsx_adapter import write_xlsx


@dataclass(frozen=True)
class Config:
    model: str = "fake-model"


class FakeProvider:
    provider_name = "fake"
    prompt_version = "fake-v1"
    glossary_version = "none"
    config = Config()

    def translate_unit(self, unit: TranslationUnit) -> TranslationResult:
        translation = f"EN:{unit.source_text}"
        return TranslationResult(
            unit_id=unit.id,
            translation=translation,
            provider=self.provider_name,
            model=self.config.model,
            prompt_version=self.prompt_version,
            glossary_version=self.glossary_version,
            source_hash=sha256_text(unit.source_text),
            result_hash=sha256_text(translation),
            request_count=1,
            validation_status="valid",
        )


def test_service_translates_a_safe_xlsx_copy_and_reports_hidden_sheet(tmp_path) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "translated.xlsx"
    write_xlsx(source)

    outcome = XlsxTranslationService(FakeProvider()).translate_file(
        source,
        output,
        source_language="zh-CN",
        target_language="en",
    )

    assert len(outcome.units) == 3
    assert [result.translation for result in outcome.results] == ["EN:阀门", "EN:阀门", "EN:管道"]
    assert outcome.skipped == ("HIDDEN_SHEET:Hidden",)
    assert output.is_file()


def test_a_cell_failing_a_quality_check_is_kept_with_a_warning(tmp_path) -> None:
    class Mismatched(FakeProvider):
        def translate_unit(self, unit: TranslationUnit) -> TranslationResult:
            return super().translate_unit(unit).model_copy(update={"provider": "other"})

    source, output = tmp_path / "source.xlsx", tmp_path / "translated.xlsx"
    write_xlsx(source)
    service = XlsxTranslationService(Mismatched(), max_attempts=2)
    service.translate_file(source, output, source_language="zh-CN", target_language="en")
    assert output.exists()
    assert service.warnings and "PROVIDER_MISMATCH" in str(service.warnings[0])
