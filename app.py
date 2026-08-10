#!/usr/bin/env python
"""Entry point for running the CLI without installing the package.

Equivalent to the ``ai-news`` console script: ``python app.py doctor``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ai_news_editor.cli.main import app

if __name__ == "__main__":
    app()
