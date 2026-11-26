"""PyInstaller entry script for GeoViewer.exe."""

import multiprocessing
import sys

if __name__ == "__main__":
    # Estimation runs in worker processes; a frozen exe must hand those processes to
    # multiprocessing here, before anything else (else each worker would start the app).
    multiprocessing.freeze_support()
    from geoviewer.app import main

    sys.exit(main())
