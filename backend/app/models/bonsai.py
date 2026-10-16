"""BONSAI external service adapter using the configured chat-completions protocol.

This is a protocol boundary, not a claim about the actual BONSAI deployment.
No model process is started, discovered, reconfigured or stopped here.
"""

from backend.app.models.qwen import QwenAdapter


class BonsaiAdapter(QwenAdapter):
    """Reuse the validated text-only wire protocol; tool calls remain rejected."""
