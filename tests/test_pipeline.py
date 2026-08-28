from __future__ import annotations

import json

from documentai import pipeline as pipeline_module
from documentai.cli import main
from documentai.pipeline import DocumentPipeline, collect_inputs, write_manifest


def test_pipeline_writes_all_three_formats(sample_pdf, tmp_path):
    out = tmp_path / "out"
    result = DocumentPipeline(out).run(sample_pdf)

    assert result.ok, result.error
    assert set(result.outputs) == {"text", "markdown", "json"}
    assert (out / "sample.txt").exists()
    assert (out / "sample.md").exists()
    assert json.loads((out / "sample.json").read_text(encoding="utf-8"))["page_count"] == 2
    assert result.converted is False and result.page_count == 2


def test_non_pdf_input_is_converted_first(sample_png, tmp_path):
    out = tmp_path / "out"
    result = DocumentPipeline(out, formats=["text"], keep_pdf=True).run(sample_png)

    assert result.ok, result.error
    assert result.strategy == "pymupdf" and result.converted is True
    assert result.pdf == out / "pdf" / "picture.pdf" and result.pdf.exists()
    assert (out / "picture.txt").exists()


def test_failures_are_reported_not_raised(tmp_path):
    bad = tmp_path / "mystery.zzz"
    bad.write_bytes(b"x")

    result = DocumentPipeline(tmp_path / "out").run(bad)

    assert result.ok is False
    assert "no conversion strategy" in result.error


def test_colliding_stems_get_unique_outputs(tmp_path, sample_pdf):
    nested = tmp_path / "nested"
    nested.mkdir()
    other = nested / "sample.pdf"
    other.write_bytes(sample_pdf.read_bytes())

    out = tmp_path / "out"
    results = DocumentPipeline(out, formats=["text"]).run_many([sample_pdf, other])

    assert all(r.ok for r in results)
    assert results[0].outputs["text"].name == "sample.txt"
    assert results[1].outputs["text"].name == "sample_1.txt"


def test_extracted_images_are_linked_relatively(illustrated_pdf, tmp_path):
    out = tmp_path / "out"
    result = DocumentPipeline(out, formats=["markdown"], extract_images=True).run(illustrated_pdf)

    assert result.ok, result.error
    assert result.images, "expected the embedded image to be extracted"
    markdown = result.outputs["markdown"].read_text(encoding="utf-8")
    assert str(out) not in markdown  # no absolute path leaks into the Markdown
    for image in result.images:
        assert image.parent == out / "images" / "illustrated"
        assert f"images/illustrated/{image.name}" in markdown


def test_output_refuses_to_overwrite_its_own_input(sample_pdf, monkeypatch):
    """No supported input extension collides with .txt/.md/.json any more, so the
    guard is provoked by pretending the text output is written as .pdf."""
    monkeypatch.setattr(pipeline_module, "OUTPUT_FORMATS", {"text": ".pdf"})

    result = DocumentPipeline(sample_pdf.parent, formats=["text"]).run(sample_pdf)

    assert result.ok is False
    assert "overwrite the input" in result.error
    assert sample_pdf.stat().st_size > 0  # the input is still intact


def test_collect_inputs_filters_and_recurses(tmp_path, sample_pdf, sample_png):
    (tmp_path / "ignore.zzz").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("plain text is not an input", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep.pdf").write_bytes(sample_pdf.read_bytes())

    flat = collect_inputs([tmp_path])
    assert sample_pdf in flat and sample_png in flat
    assert not any(p.suffix in (".zzz", ".txt") for p in flat)
    assert sub / "deep.pdf" not in flat

    deep = collect_inputs([tmp_path], recursive=True)
    assert sub / "deep.pdf" in deep


def test_manifest_summarises_the_run(tmp_path, sample_pdf):
    out = tmp_path / "out"
    results = DocumentPipeline(out, formats=["text"]).run_many([sample_pdf])
    manifest = write_manifest(results, out / "manifest.json")

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["documents"] == 1 and payload["succeeded"] == 1
    assert payload["results"][0]["outputs"]["text"].endswith("sample.txt")


def test_manifest_never_clobbers_a_documents_json(tmp_path, sample_pdf):
    twin = tmp_path / "manifest.pdf"
    twin.write_bytes(sample_pdf.read_bytes())
    out = tmp_path / "out"

    results = DocumentPipeline(out, formats=["json"]).run_many([twin])
    written = write_manifest(results, out / "manifest.json")

    assert written == out / "manifest_run.json"
    document = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert document["page_count"] == 2  # the document's own JSON survived


def test_cli_end_to_end(tmp_path, sample_pdf, capsys):
    out = tmp_path / "cli-out"
    code = main([str(sample_pdf), "-o", str(out), "-f", "text", "md", "--manifest"])

    assert code == 0
    assert (out / "sample.txt").exists() and (out / "sample.md").exists()
    assert (out / "manifest.json").exists()
    assert "1/1 document(s) processed" in capsys.readouterr().out


def test_cli_returns_nonzero_on_failure(tmp_path, capsys):
    bad = tmp_path / "bad.zzz"
    bad.write_bytes(b"x")
    assert main([str(bad), "-o", str(tmp_path / "out")]) == 1
    assert "FAIL" in capsys.readouterr().err
