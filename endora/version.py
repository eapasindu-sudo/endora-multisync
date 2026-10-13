"""Version and update source. The GitHub build writes _build_version.py with the real number."""
try:
    from ._build_version import VERSION  # type: ignore
except ImportError:
    VERSION = "1.1.0"

# public repo whose GitHub Releases carry the installers
UPDATE_REPO = "eapasindu-sudo/endora-multisync"
