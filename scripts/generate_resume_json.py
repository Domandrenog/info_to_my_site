#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ucoin_to_mysite.generate_resume_json import main


if __name__ == "__main__":
    asyncio.run(main())
