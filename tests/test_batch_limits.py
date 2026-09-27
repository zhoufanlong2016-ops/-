from types import SimpleNamespace

from document_translator.providers.batch_limits import model_batch_characters, split_semantic_batches


def test_model_limits_are_provider_aware_and_overrideable():
    assert model_batch_characters("qwen-plus") < model_batch_characters("gpt-5.6-sol")
    assert model_batch_characters("qwen-max") > model_batch_characters("qwen-plus")
    assert model_batch_characters("qwen-plus", 700) == 700


def test_semantic_batch_split_keeps_units_whole():
    units = [SimpleNamespace(source_text="a" * 80), SimpleNamespace(source_text="b" * 80), SimpleNamespace(source_text="c" * 80)]
    batches = split_semantic_batches(units, model="qwen-plus", explicit_limit=140, overhead=20)
    assert [[unit.source_text[0] for unit in batch] for batch in batches] == [["a"], ["b"], ["c"]]
