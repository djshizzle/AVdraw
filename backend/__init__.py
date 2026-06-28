"""AV Schematic Builder — backend package.

A thin service layer over the existing ``src/`` pipeline
(BOM → draw.io → EasySchematic). The domain model and the single-room core
loop are stdlib-only so they run without any web framework installed; FastAPI
is an optional transport layer (see ``backend.main``).
"""

__version__ = "0.1.0"
