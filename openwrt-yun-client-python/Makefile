# Makefile para openwrt-yun-client-python (Python client, system Python only)
.PHONY: all clean wheel

all: wheel


clean:
	rm -rf dist build *.egg-info bin yunbridge_client/pubsub_plugin.py

wheel:
	@echo "[WHEEL] Compilando openwrt-yun-client-python (.whl)"
	python3 -m build --wheel
	mkdir -p bin
	cp dist/*.whl bin/
