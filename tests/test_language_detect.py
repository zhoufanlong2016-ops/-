from pathlib import Path

import fitz

from document_translator.language_detect import detect_document_language, detect_text_language, other_language


def test_chinese_with_english_abbreviations_is_chinese():
    text = "关于修订中土集团福州勘察设计研究院有限公司国内项目经营管理办法的通知，EPC 项目由 PMC 负责。" * 3
    assert detect_text_language(text) == "zh"


def test_english_with_a_chinese_stamp_is_english():
    text = "The Contractor shall design and provide a clean gas-based fire suppression system. " * 5 + "印章"
    assert detect_text_language(text) == "en"


def test_bilingual_or_empty_text_is_undetermined():
    assert detect_text_language("") is None
    assert detect_text_language("12.5 0.00 71.719") is None
    bilingual = "清表面积 Clearing area 清表长度 Clearing length 施工图 construction drawing " * 4
    assert detect_text_language(bilingual) is None


def test_other_language():
    assert other_language("zh") == "en" and other_language("en") == "zh"


def test_pdf_sample(tmp_path: Path):
    path = tmp_path / "doc.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 80), "Employer's Requirements for the sewerage system. " * 4, fontsize=8)
    doc.save(path)
    assert detect_document_language(path) == "en"
