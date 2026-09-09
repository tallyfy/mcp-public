"""Template path tester: walk every path through a template, report problems not paths.

Spec: tallyfy/mcp#997. Queue entry: tallyfy/work-queue#923.

The engine is a **pure function** from one template document to a findings list.
No SDK object, no HTTP client, no LLM, no clock, no randomness. That is what lets
the same engine run inside an MCP tool, inside a CLI, and inside a test, and it is
why the conformance fixtures under ``fixtures/template-tests/`` can be the shared
reference between this Python implementation and the Go one (spec section 8).

Nothing in this package may import ``fastmcp``, ``tallyfy`` or anything under
``server/tools``. ``tests/unit/server/template_testing/test_engine.py`` fails if
that changes.

Import the entry points from their own modules::

    from template_testing.engine import test_template_document, run_scenario
    from template_testing.mermaid import render_mermaid

They are deliberately NOT re-exported here: ``server/tools/automation.py`` imports
one helper from ``template_testing.references`` and must not pay for the whole
engine at import time.
"""
