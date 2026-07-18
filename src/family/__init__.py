"""Deterministic family-coordination checks (issue #160).

Two one-shot scheduled checks living alongside the WhatsApp/Gmail message
pipeline, reusing this app's run store, notify, config, and UI — but not its
message-analysis core. All detection/decision logic is pure Python (see
:mod:`src.family.rules`); no LLM or agent runtime is in either loop.
"""
