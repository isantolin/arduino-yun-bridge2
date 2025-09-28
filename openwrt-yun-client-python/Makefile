#
# This file is part of Arduino Yun Ecosystem v2.
#
# Copyright (C) 2025 Ignacio Santolin and contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Makefile para openwrt-yun-client-python (Python client, system Python only)
.PHONY: all clean wheel

all: wheel


clean:
	rm -rf dist build *.egg-info bin

wheel:
	@echo "[WHEEL] Compilando openwrt-yun-client-python (.whl)"
	python3 -m build --wheel
	mkdir -p bin
	cp dist/*.whl bin/
