import sys

if sys.version_info < (3, 11):
    sys.exit("weftindex needs Python 3.11 or newer (for tomllib)")
