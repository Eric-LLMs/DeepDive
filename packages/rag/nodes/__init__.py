"""Pipeline node package: one file per stage.

Adding a node = create a file in this package + register its class in
``rag.pipeline.registry._import_and_register`` + add the node name to the pipeline config.
Node modules must stay dependency-light (they may import other rag modules and
``core.ports``, but never ``rag.pipeline.executor`` / ``rag.pipeline.factory``) so the registry can load
them without an import cycle.
"""
