# Need to make the ndpulsegen module accessable. No need to do this if module is already accessable to your python environment
import sys
from pathlib import Path
current_file_path = Path(__file__).resolve()
sys.path.insert(0, str(current_file_path.parent.parent / 'src'))

import ndpulsegen
ndpulsegen.gui.main()


