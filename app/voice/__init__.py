"""Stephanie.ai voice launch gate.

Implements the launch-critical surface required by the Live Voice Launch
Gate v1 spec: real STT/TTS configuration checks, WebSocket capability,
transcript persistence, audit hash chain, kill switch, and avatar session
state. Nothing in this package returns PASS unless the underlying
capability is actually verifiable from configuration / live dependencies.
"""
