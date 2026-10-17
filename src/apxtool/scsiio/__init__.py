import sys

if sys.platform == 'win32':
    from .win32port import SCSIDev

elif sys.platform == 'linux':
    from .linuxport import SCSIDev

else:
    raise ImportError("Platform '%s' is not supported (yet)" % sys.platform)
