from pathlib import Path

from pdf_factory import text_pdf_bytes


source = Path("/workspace/services/contract/tests/stage5_neutral_demo.txt")
target = Path("/artifacts/stage5-neutral-demo.pdf")
target.write_bytes(text_pdf_bytes(source.read_text("utf-8").replace("\n", " ")))
