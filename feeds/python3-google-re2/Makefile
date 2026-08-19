include $(TOPDIR)/rules.mk

PKG_NAME:=python3-google-re2
PKG_VERSION:=1.1.20251105
PKG_RELEASE:=1

PYPI_NAME:=google_re2
PKG_HASH:=1db14a292ee8303b91e91e7c37e05ac17d3c467f29416c79ac70a78be3e65bda

PKG_MAINTAINER:=Ignacio Santolin <isantolin@gmail.com>
PKG_LICENSE:=BSD-3-Clause
PKG_LICENSE_FILES:=LICENSE

PKG_BUILD_DEPENDS:=python-pybind11/host abseil-cpp

include $(TOPDIR)/feeds/packages/lang/python/pypi.mk
include $(INCLUDE_DIR)/package.mk
include $(TOPDIR)/feeds/packages/lang/python/python3-package.mk

# The re2/re2.h header and libre2 are installed by the "re2" system package
# into STAGING_DIR/usr/{include,lib}, but setup.py links against them via a
# plain setuptools Extension (libraries=['re2']), without pkg-config. The
# python3-package.mk build only injects the Python include dir by default,
# so the re2 sysroot paths must be added explicitly here.
#
# The google-re2 sdist's own C++ extension (_re2.cc) also includes Abseil
# headers directly (e.g. absl/strings/string_view.h), independent of
# whatever Abseil version libre2 itself was built against, so "abseil-cpp"
# must be a build dependency too to populate STAGING_DIR/usr/include/absl.
TARGET_CFLAGS += -I$(STAGING_DIR)/usr/include
TARGET_LDFLAGS += -L$(STAGING_DIR)/usr/lib

define Package/python3-google-re2
	SECTION:=lang
	CATEGORY:=Languages
	SUBMENU:=Python
	TITLE:=RE2 Python bindings
	URL:=https://github.com/google/re2
	DEPENDS:=+python3 +re2
endef

define Package/python3-google-re2/description
 google-re2 provides Python bindings for RE2, Google's fast, safe,
 linear-time regular expression library, built with pybind11.
endef

$(eval $(call Py3Package,python3-google-re2))
$(eval $(call BuildPackage,python3-google-re2))
