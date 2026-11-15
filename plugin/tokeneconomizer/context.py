"""The `LiveContext` each Hermes runtime hands upstream: its own runner and job spawning.

Upstream builds a context per hook call or job (`LiveContext.build`); `context_for(runtime)`
returns a subclass bound to one runtime, so the side-call runner and detached jobs belong to the
profile whose hook created them. Nothing process-wide is patched for this.
"""
from tokeneconomizer.plugin.context import LiveContext


def context_for(runtime, generation=None):
    """A `LiveContext` subclass bound to `runtime` (and, for a job, to the generation it was
    queued under, so a job that outlives an off/unload cannot run a model call)."""

    class HermesContext(LiveContext):
        _rt = runtime
        _gen = generation

        def side_runner(self):
            from .runner import Binding, OptimizerRunner
            rt, gen = self._rt, self._gen
            live = ((lambda: rt.live(gen) and self._enabled_now()) if gen is not None
                    else (lambda: False))
            # A hook-time context (no job generation) never calls a model itself: fold and
            # judge run as jobs, whose contexts carry their generation.
            return OptimizerRunner(Binding(rt.llm, rt.root, live), self.config)

        def _enabled_now(self):
            """The merged config as it is now (`te config set enabled=false` mid-job)."""
            import os
            from tokeneconomizer.plugin.config import load_config
            try:
                return load_config(self.root, repo_dir=os.path.dirname(self.store.base),
                                   env=self.env).enabled
            except Exception:
                return False

        def spawn(self, argv, log_name):
            return self._rt.spawn(self, argv, log_name)

    return HermesContext
