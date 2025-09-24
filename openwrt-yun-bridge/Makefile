# Makefile para openwrt-yun-bridge (Python daemon, system Python only)
.PHONY: all clean wheel

all: wheel

clean:
	rm -rf dist build *.egg-info bin

wheel:
	@echo "[WHEEL] Compilando openwrt-yun-bridge (.whl)"
	python3 -m build --wheel
	mkdir -p bin
	cp dist/*.whl bin/
