"""HTTP routing, one module per resource.

Each module owns its own URL prefix and OpenAPI tag, so adding an endpoint means
touching one file and `main.py` never grows. docs/HLD.md §10.2 lists 14 endpoints;
blocks E–G add the rest.
"""
