"""Minimal node-preserving PPTX text translation service."""
from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile
import hashlib
import re
import xml.etree.ElementTree as ET

from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id, sha256_text
from document_translator.font_policy import CJK_FONT, latin_font_for
from document_translator.translation_rules import rule_protected_tokens
from document_translator.core import TranslationResult, validate_result_for_unit

_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_A_T = f"{{{_A_NS}}}t"


class PptxTranslationService:
    def __init__(self, provider):
        self.provider = provider

    @staticmethod
    def _should_translate(text: str, target_language: str) -> bool:
        if not text.strip():
            return False
        target = target_language.casefold()
        if target in {"zh", "zh-cn", "zh-hans", "zh-sg"}:
            # A Chinese slide commonly contains immutable English acronyms,
            # standards and place names (for example, "EPC施工总承包").
            # Sending the whole mixed paragraph to an en->zh engine rewrites
            # the Chinese source and breaks protected-token validation.  This
            # pass owns complete English paragraphs only; mixed paragraphs
            # remain intact until a range-preserving mixed-script translator
            # is implemented.
            return bool(re.search(r"[A-Za-z]", text)) and not bool(
                re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text),
            )
        if target in {"en", "en-us", "en-gb"}:
            return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
        return True

    def translate_file(self, source: str | Path, destination: str | Path, *, source_language: str, target_language: str):
        source, destination = Path(source), Path(destination)
        if source.resolve() == destination.resolve():
            raise ValueError("source and destination paths must differ")
        document_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        translated = 0
        with ZipFile(source) as zin, NamedTemporaryFile(dir=destination.parent, suffix=".pptx.tmp", delete=False) as tmp:
            temp_path = Path(tmp.name)
            entries = []
            pending_all = []
            for info in zin.infolist():
                    data = zin.read(info.filename)
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", info.filename):
                        root = ET.fromstring(data)
                        for ordinal, paragraph in enumerate(root.findall(f".//{{{_A_NS}}}p")):
                            nodes = paragraph.findall(f".//{{{_A_NS}}}t")
                            text = "".join(node.text or "" for node in nodes)
                            if not self._should_translate(text, target_language):
                                continue
                            location = DocumentLocation(part=info.filename, object_id=f"paragraph:{ordinal}", node_ids=[info.filename, str(ordinal)])
                            data_model = dict(document_hash=document_hash, format=DocumentFormat.PPTX, location=location,
                                              source_language=source_language, target_language=target_language, source_text=text,
                                              protected_tokens=rule_protected_tokens(text), style_signature="", context_before="", context_after="")
                            unit = TranslationUnit(id=generate_unit_id(**data_model), **data_model)
                            pending_all.append((paragraph, nodes, unit))
                        entries.append((info, root))
                    elif re.fullmatch(r"ppt/diagrams/data\d+\.xml", info.filename):
                        root = ET.fromstring(data)
                        for ordinal, point in enumerate(root.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/diagram}pt")):
                            nodes = point.findall(f".//{{{_A_NS}}}t")
                            text = "".join(x.text or "" for x in nodes)
                            if not self._should_translate(text, target_language):
                                continue
                            location = DocumentLocation(part=info.filename, object_id=f"point:{ordinal}", node_ids=[info.filename, str(ordinal)])
                            data_model = dict(document_hash=document_hash, format=DocumentFormat.PPTX, location=location,
                                              source_language=source_language, target_language=target_language, source_text=text,
                                              protected_tokens=rule_protected_tokens(text), style_signature="", context_before="", context_after="")
                            pending_all.append((point, nodes, TranslationUnit(id=generate_unit_id(**data_model), **data_model)))
                        entries.append((info, root))
                    else:
                        entries.append((info, data))
            if pending_all:
                # One document-wide queue lets the provider fill each request
                # to its safe limit instead of issuing one request per slide.
                self._apply_batch(pending_all)
                translated = len(pending_all)
            with ZipFile(tmp, "w", ZIP_DEFLATED) as zout:
                for info, value in entries:
                    if isinstance(value, ET.Element) and re.fullmatch(r"ppt/slides/slide\d+\.xml", info.filename):
                        self._apply_target_font_to_unchanged_latin(value, target_language)
                    data = ET.tostring(value, encoding="utf-8", xml_declaration=True) if isinstance(value, ET.Element) else value
                    zout.writestr(info, data)
        temp_path.replace(destination)
        return translated

    def _apply_batch(self, pending):
        units = [item[2] for item in pending]
        results = self.provider.translate_batch(units) if hasattr(self.provider, "translate_batch") else [self.provider.translate_unit(u) for u in units]
        if len(results) != len(pending):
            raise ValueError("batch translation count mismatch")
        if any(not isinstance(result, TranslationResult) for result in results):
            raise ValueError("invalid batch translation result type")
        mapped = {result.unit_id: result for result in results}
        if len(mapped) != len(results) or set(mapped) != {unit.id for unit in units}:
            raise ValueError("batch translation ID mismatch or duplicate IDs")
        # Validate the entire batch before changing any text or formatting.
        for unit in units:
            result = mapped[unit.id]
            errors = validate_result_for_unit(unit, result)
            if errors:
                raise ValueError("invalid batch translation: " + "; ".join(errors))
            if not result.translation.strip():
                raise ValueError("empty batch translation")
        for paragraph, nodes, _unit in pending:
            result = mapped[_unit.id]
            translated = self._preserve_edge_whitespace(_unit.source_text, result.translation)
            nodes[0].text = translated.replace("\u2011", "-").replace("\u2014", "-")
            for node in nodes[1:]:
                node.text = ""
            self._apply_font_policy(paragraph, translated)

    @staticmethod
    def _preserve_edge_whitespace(source: str, translation: str) -> str:
        """Keep layout-significant leading/trailing whitespace from source."""
        leading = re.match(r"^\s*", source).group(0)
        trailing = re.search(r"\s*$", source).group(0)
        core = translation.strip()
        return f"{leading}{core}{trailing}"


    @staticmethod
    def _apply_font_policy(paragraph, translation: str) -> None:
        """Force translated runs to the requested Chinese/English fonts."""
        latin = latin_font_for(translation)
        run_properties = []
        for run in paragraph.findall(f".//{{{_A_NS}}}r"):
            rpr = run.find(f"./{{{_A_NS}}}rPr")
            if rpr is None:
                rpr = ET.Element(f"{{{_A_NS}}}rPr")
                run.insert(0, rpr)
            run_properties.append(rpr)
        end_rpr = paragraph.find(f"./{{{_A_NS}}}endParaRPr")
        if end_rpr is not None:
            run_properties.append(end_rpr)
        for rpr in run_properties:
            for tag, value in (("latin", latin), ("ea", CJK_FONT), ("cs", latin), ("sym", latin)):
                node = rpr.find(f"./{{{_A_NS}}}{tag}")
                if node is None:
                    node = ET.SubElement(rpr, f"{{{_A_NS}}}{tag}")
                node.set("typeface", value)

    @classmethod
    def _apply_target_font_to_unchanged_latin(cls, root, target_language: str) -> None:
        """Apply the English fallback font to acronym-only untouched paragraphs."""
        if target_language.casefold() not in {"en", "en-us", "en-gb", "english"}:
            return
        for paragraph in root.findall(f".//{{{_A_NS}}}p"):
            text = "".join(node.text or "" for node in paragraph.findall(f".//{{{_A_NS}}}t"))
            if re.search(r"[A-Za-z]", text) and not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text):
                cls._apply_font_policy(paragraph, text)
