"""
qmemory.nanobot — NanoBot tool entry points for Rakeezah integration.

These are thin wrappers that expose the same 6 Qmemory tools as the MCP
server, but packaged for the nanobot-ai SDK used inside Rakeezah (FastAPI).

Tools are registered via entry points in pyproject.toml under the
[project.entry-points."nanobot.tools"] group — nanobot-ai discovers them
automatically at startup without any manual registration step.

Each tool class:
  - Inherits from nanobot.tools.BaseTool (guarded import — safe without SDK)
  - Declares name, description, parameters (JSON Schema)
  - Implements async run(**kwargs) that calls the matching core function

Available tools:
  QmemoryBootstrapTool  — bootstrap.py  — calls assemble_context()
  QmemorySearchTool     — search.py     — calls search_memories()
  QmemorySaveTool       — save.py       — calls save_memory()
  QmemoryCorrectTool    — correct.py    — calls correct_memory()
  QmemoryLinkTool       — link.py       — calls link_nodes()
  QmemoryPersonTool     — person.py     — calls create_person()
"""
