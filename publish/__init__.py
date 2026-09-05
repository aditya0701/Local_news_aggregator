"""Layer 2: the delivery format the frontend reads.

`output/articles_hindi.json` stays the source of truth (the pipeline appends to
it, the eval harnesses read it). This package derives the browser-facing files
from it so a reader never downloads the whole archive to see one page.
"""

from publish.delivery import build_delivery

__all__ = ["build_delivery"]
