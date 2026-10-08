"""Entry script for PyInstaller (it needs a plain script, not ``python -m``)."""

from mourse_decoder.app import main

raise SystemExit(main())
