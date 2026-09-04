"""Streamlit front end for the DocumentAI pipeline.

Deployed on Streamlit Community Cloud with `app.py` as the entry point. Uploaded
files are processed in a temporary directory and the outputs are read straight
into memory, so nothing is left on the server's disk between runs.
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

from documentai import DocumentPipeline, supported_extensions, unique_stems
from documentai.converters import find_soffice
from documentai.formats import OFFICE_EXTS
from documentai.parsers import OUTPUT_FORMATS

PREVIEW_LIMIT = 20_000  # characters rendered inline before truncating


# --------------------------------------------------------------------------- #
# Processing
# --------------------------------------------------------------------------- #


@st.cache_data(show_spinner=False, max_entries=32)
def process(
    filename: str,
    data: bytes,
    formats: tuple[str, ...],
    extract_images: bool,
    spans: bool,
    keep_pdf: bool,
    stem: str | None = None,
) -> dict:
    """Run one uploaded file through the pipeline, returning everything in memory.

    Cached on the arguments, so re-rendering the page (a download click, a tab
    switch) never re-converts a document. ``stem`` names the outputs; the
    caller keeps it unique across the batch (see :func:`unique_stems`) because
    every call here runs its own pipeline.
    """
    with tempfile.TemporaryDirectory(prefix="documentai-app-") as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / "input" / filename
        source.parent.mkdir(parents=True)
        source.write_bytes(data)

        pipeline = DocumentPipeline(
            tmp_path / "output",
            formats=list(formats),
            extract_images=extract_images,
            spans=spans,
            keep_pdf=keep_pdf,
        )
        result = pipeline.run(source, stem=stem)

        payload = {
            "name": filename,
            "stem": result.stem,
            "ok": result.ok,
            "error": result.error,
            "strategy": result.strategy,
            "converted": result.converted,
            "page_count": result.page_count,
            "duration": result.duration,
            "outputs": {},
            "images": {},
            "pdf": None,
        }
        if result.ok:
            payload["outputs"] = {
                fmt: path.read_text(encoding="utf-8") for fmt, path in result.outputs.items()
            }
            payload["images"] = {img.name: img.read_bytes() for img in result.images}
            if keep_pdf and result.pdf:
                payload["pdf"] = result.pdf.read_bytes()
        return payload


def build_zip(payloads: list[dict]) -> bytes:
    """Bundle every successful document's outputs into one archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for payload in payloads:
            if not payload["ok"]:
                continue
            stem = payload["stem"]
            for fmt, content in payload["outputs"].items():
                archive.writestr(f"{stem}{OUTPUT_FORMATS[fmt]}", content)
            for name, blob in payload["images"].items():
                archive.writestr(f"images/{stem}/{name}", blob)
            if payload["pdf"]:
                archive.writestr(f"pdf/{stem}.pdf", payload["pdf"])
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #


def sidebar() -> dict:
    """Conversion options, plus a note on what this deployment can convert."""
    with st.sidebar:
        st.header("Options")
        formats = st.multiselect(
            "Output formats",
            options=list(OUTPUT_FORMATS),
            default=list(OUTPUT_FORMATS),
            format_func=lambda f: f"{f} ({OUTPUT_FORMATS[f]})",
        )
        # Images are written while extracting Markdown, so the option is moot
        # without it; JSON font detail is likewise moot without JSON.
        settings = {
            "formats": formats,
            "extract_images": st.checkbox(
                "Extract embedded images", value=False,
                disabled="markdown" not in formats,
                help="Writes the images alongside the Markdown, which links to them. "
                     "Requires the markdown format.",
            ),
            "spans": st.checkbox(
                "Font detail in JSON", value=True,
                disabled="json" not in formats,
                help="Per-span font, size, weight and colour. "
                     "Turn off for much smaller JSON.",
            ),
            "keep_pdf": st.checkbox(
                "Include the intermediate PDF", value=False,
                help="Adds the converted PDF to the download bundle.",
            ),
        }

        st.divider()
        if find_soffice():
            st.success("LibreOffice detected — Office formats supported.", icon="✅")
        else:
            st.warning(
                "LibreOffice is not installed, so Office formats "
                f"({', '.join(sorted(OFFICE_EXTS))}) cannot be converted. "
                "Everything else works.",
                icon="⚠️",
            )
        with st.expander("Supported inputs"):
            st.write(" ".join(supported_extensions()))
    return settings


def show_document(payload: dict) -> None:
    """Metrics, previews and download buttons for one processed document."""
    stem = payload["stem"]  # unique per document, so widget keys never collide
    left, middle, right = st.columns(3)
    left.metric("Pages", payload["page_count"])
    middle.metric(
        "Conversion",
        payload["strategy"] if payload["converted"] else "none (already PDF)",
    )
    right.metric("Time", f"{payload['duration']:.1f}s")

    for fmt, content in payload["outputs"].items():
        st.subheader(f"{fmt} · `{stem}{OUTPUT_FORMATS[fmt]}`")
        st.download_button(
            f"⬇️ {stem}{OUTPUT_FORMATS[fmt]}",
            data=content,
            file_name=f"{stem}{OUTPUT_FORMATS[fmt]}",
            mime="application/json" if fmt == "json" else "text/plain",
            key=f"download-{stem}-{fmt}",
        )

        truncated = content[:PREVIEW_LIMIT]
        if fmt == "json":
            st.json(json.loads(content), expanded=False)
        elif fmt == "markdown":
            # Image links point into the download bundle, so they do not
            # resolve in this preview.
            rendered, source_view = st.tabs(["Rendered", "Source"])
            rendered.markdown(truncated)
            source_view.code(truncated, language="markdown")
        else:
            st.code(truncated, language="text")

        if len(content) > PREVIEW_LIMIT:
            st.caption(
                f"Preview truncated at {PREVIEW_LIMIT:,} of {len(content):,} "
                "characters — the download holds the full document."
            )

    if payload["images"]:
        st.subheader(f"Images ({len(payload['images'])})")
        columns = st.columns(min(4, len(payload["images"])))
        for index, (name, blob) in enumerate(payload["images"].items()):
            columns[index % len(columns)].image(
                blob, caption=name, use_container_width=True
            )


def main() -> None:
    st.set_page_config(page_title="DocumentAI", page_icon="📄", layout="wide")
    settings = sidebar()

    st.title("📄 DocumentAI")
    st.caption(
        "Convert any document to PDF, then extract plain text, Markdown and "
        "structured JSON. Built on PyMuPDF."
    )

    uploads = st.file_uploader(
        "Documents",
        type=[ext.lstrip(".") for ext in supported_extensions()],
        accept_multiple_files=True,
        help="PDFs are parsed directly; everything else is converted to PDF first.",
    )

    if not settings["formats"]:
        st.error("Pick at least one output format in the sidebar.")
        return
    if not uploads:
        st.info("Upload one or more documents to get started.", icon="👆")
        return

    payloads = []
    progress = st.progress(0.0, text="Processing…")
    # Each upload is processed (and cached) on its own, so distinct output
    # names for e.g. report.pdf + report.docx have to be decided here.
    stems = unique_stems([upload.name for upload in uploads])
    for index, (upload, stem) in enumerate(zip(uploads, stems, strict=True)):
        progress.progress(index / len(uploads), text=f"Processing {upload.name}…")
        payloads.append(
            process(
                upload.name,
                upload.getvalue(),
                tuple(settings["formats"]),
                settings["extract_images"],
                settings["spans"],
                settings["keep_pdf"],
                stem=stem,
            )
        )
    progress.empty()

    for payload in (p for p in payloads if not p["ok"]):
        st.error(f"**{payload['name']}** — {payload['error']}", icon="🚫")

    succeeded = [p for p in payloads if p["ok"]]
    if not succeeded:
        return

    if len(succeeded) > 1:
        st.download_button(
            f"⬇️ Download all {len(succeeded)} documents (.zip)",
            data=build_zip(succeeded),
            file_name="documentai-output.zip",
            mime="application/zip",
            type="primary",
        )

    for tab, payload in zip(st.tabs([p["name"] for p in succeeded]), succeeded, strict=True):
        with tab:
            show_document(payload)


if __name__ == "__main__":
    main()
