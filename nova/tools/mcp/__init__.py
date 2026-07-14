"""Google Workspace remote MCP integrations (Calendar, Gmail, People, Drive).

Auth lives in ``oauth``; JSON-RPC transport in ``client``. Individual products
expose ``NovaTool`` wrappers that degrade cleanly when OAuth is missing.
"""
