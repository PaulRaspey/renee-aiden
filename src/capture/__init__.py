"""Session capture package.

Owns the post-Part-1 session pipeline: recorder, triage, review notes,
publishing. Everything under this package writes into a
RENEE_SESSIONS_DIR root (default C:\\Users\\Epsar\\renee-sessions) on the
OptiPlex; none of it runs pod-side.
"""
