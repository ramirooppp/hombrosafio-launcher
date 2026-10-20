import os
import sys

# Ensure current directory is in path
base_dir = os.path.dirname(os.path.abspath(__file__))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from ui.main_window import HombrosafioLauncher

if __name__ == "__main__":
    app = HombrosafioLauncher()
    app.mainloop()
