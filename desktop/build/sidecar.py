"""Frozen entry point exposing normal Penstock commands and the desktop model helper."""
import os
import sys


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    if sys.argv[1:2] == ["desktop-models"]:
        import models
        sys.exit(models.main(sys.argv[2:]))
    import penstock
    try:
        penstock.main()
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
