"""Isolated sandbox service.

Runs ALL untrusted, model-generated code — both test execution and live
previews — in a process that holds no application secrets. The main app
(``src.dev_agent``) reaches it over the internal docker network and reverse-
proxies preview traffic through it, so generated code never shares a process
with the LLM/database/Redis credentials.
"""
