"""UAHP supervisor hardening.

Ships the trust primitives that sit on top of `src.identity.uahp_identity`:
death certificates with cause + task_id, task-failure certificates, a
dead-agent registry that rejects post-death heartbeats, a replay-detection
ledger, memory-vault wiring, and a QAL attestation chain.

The base `AgentIdentity` (HMAC-SHA256 sign/verify) lives in
`src.identity.uahp_identity`; every module here imports from there so there is
exactly one crypto primitive across the stack.
"""
