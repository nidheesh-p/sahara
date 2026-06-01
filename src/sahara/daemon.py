# Module alias — canonical location: sahara.sync.daemon
import importlib
import sys

sys.modules[__name__] = importlib.import_module('sahara.sync.daemon')
