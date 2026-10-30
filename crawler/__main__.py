import sys

if sys.version_info < (3, 10):
    sys.exit(f"steam4cgs requires Python 3.10+, but this is {sys.version.split()[0]}")

from crawler.cli import main  # noqa: E402

sys.exit(main())
