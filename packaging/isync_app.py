"""PyInstaller giriş noktası: iSync.exe"""

import multiprocessing

from isync.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
