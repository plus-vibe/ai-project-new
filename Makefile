PYTHON ?= python3
.PHONY: harness-check inspect
harness-check:
	$(PYTHON) tools/check_harness.py
inspect:
	$(PYTHON) tools/inspect_zip.py "$(INPUT)"
