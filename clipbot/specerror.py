"""The one exception type shared by the spec modules.

It lives here rather than in `reelspec.py` so that `fxspec.py` can raise it
without importing `reelspec`, and `reelspec` can import `fxspec` without a
cycle. `reelspec` re-exports it, so every existing
`from ..reelspec import SpecError` keeps working unchanged.

Subclassing ValueError is load-bearing: the PATCH route in server/app.py maps
ValueError to a 400, so an invalid spec reaches the UI as a real error message
instead of a silent no-op.
"""


class SpecError(ValueError):
    """A spec that cannot be resolved into a valid frame."""
