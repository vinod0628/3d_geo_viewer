"""PyInstaller entry script for GeoViewer.exe."""

import sys

from geoviewer.app import main

if __name__ == "__main__":
    sys.exit(main())
