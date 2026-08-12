"""Security primitives shared across the API.

Currently holds credential encryption at rest (phase 4a). Key handling lives
in ``credential_crypto`` and nowhere else — no other module imports ``Fernet``.
"""
