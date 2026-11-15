"""`PrefixStage`: the micro-skill lifecycle hook -- render before a session, learn after it.

It builds the distiller the profile names -- the LLM-free environment facts (`envfacts`), the
paid proposer (`distiller`), or both (`envfacts+distiller`, the free layer first and its claims
handed to the proposer as already stated) -- renders its block for the next session, and folds
each finished session back in, reporting the paid proposer's cost for that session.
"""
import os

from ..stage import Stage
from . import distiller as _distiller
from .distiller import Distiller


# ---- composition: the free environment layer under the paid manual -------------------
class ComposedDistiller:
    """`envfacts` first, the paid manual on top, driven as one distiller.

    The environment layer renders first (its block is what the proposer is told not to
    restate); both fold every finished task; each persists in its own state dir. Overhead is
    the proposer's alone -- the environment layer is LLM-free by construction."""
    def __init__(self, env, dist):
        self.env, self.dist = env, dist

    @property
    def opt_tokens(self):
        return self.dist.opt_tokens

    @property
    def now(self):
        return self.dist.now

    def render(self, task=None):
        a = self.env.render(task)
        b = _without_retired(self.dist.render(task), self.env)        # [hermes patch P8]
        return (a + ("\n" if a and b else "") + b) if (a or b) else ""

    def injected_ids(self):
        return self.env.injected_ids()

    def seed_injected(self, ids):
        self.env.seed_injected(ids)

    def update(self, transcript_path, iid="", resolved=None, edited=None):
        self.env.update(transcript_path, iid=iid, resolved=resolved, edited=edited)
        self.dist.update(transcript_path, iid=iid, resolved=resolved, edited=edited)

    def save(self, root):
        self.env.save(root)
        self.dist.save(root)

    def debug_row(self):
        row = dict(self.dist.debug_row())
        row.update(self.env.debug_row())
        row["_profile"] = "envfacts+distiller"
        return row


def _retired_subjects(env):
    """The subjects (commands, tools, values) of environment facts later sessions retired."""
    import re
    out = set()
    for f in getattr(env, "facts", {}).values():
        if getattr(getattr(f, "life", None), "state", None) != "RETIRED":
            continue
        for v in [*str(f.id).split(":")[2:], *[str(x) for x in (f.params or {}).values()]]:
            out |= {t.lower() for t in re.findall(r"[\w.+-]{3,}", v)}
    return out - {"run_tests", "run_interpreter", "run_build", "install"}


def _without_retired(block, env):
    """[hermes patch P8] The paid manual, less any line about a subject whose free fact was
    retired: two layers learning the same belief must not keep the contradicted one alive."""
    import re
    subjects = _retired_subjects(env)
    if not block or not subjects:
        return block
    lines = block.splitlines()
    kept = [lines[0]] + [ln for ln in lines[1:]
                         if not subjects & {t.lower() for t in re.findall(r"[\w.+-]{3,}", ln)}]
    return "\n".join(kept) + "\n" if len(kept) > 1 else ""


def _capped(block, max_chars):
    """[hermes patch P8] Whole lines of `block` up to `max_chars` (the combined free + paid
    render had no cap; only the free layer did)."""
    if not block or not max_chars or len(block) <= max_chars:
        return block
    out, n = [], 0
    for ln in block.splitlines(keepends=True):
        if n + len(ln) > max_chars:
            break
        out.append(ln)
        n += len(ln)
    return "".join(out)


# ---- lifecycle stage -------------------------------------------------------
class PrefixStage(Stage):
    """Injection lifecycle hook: render the manual before a task, fold the finished
    task back into the distiller after it, and report the paid proposer's per-task
    token delta for the amortized ledger."""
    name = "prefix"

    def __init__(self, cfg, repo, results_root, fresh,
                 runner=None, optimizer_model="agent", adapter=None, agent=None):
        self.results_root = results_root
        self.adapter = adapter
        self.agent = agent
        # The proposer cadence is per distiller instance, so profiles with different settings
        # can share one process. [hermes patch P5: was a rebind of the module global]
        pe = int(getattr(cfg, "propose_every", 0) or 0)
        # Combined free + paid block: twice the free layer's token cap, ~4 chars a token.
        self.cap_chars = 8 * int(getattr(cfg, "max_tokens", 0) or 150)  # [hermes patch P8]
        profile = getattr(cfg, "profile", "distiller") or "distiller"
        # The environment-fact layer reads intents through a domain vocabulary; the adapter
        # supplies it when it has one (SWE-bench: Python/git; Web-Bench: Node/npm).
        vocab = adapter.env_vocabulary() if hasattr(adapter, "env_vocabulary") else None
        env = None
        if "envfacts" in profile:
            from .envfacts import EnvFactsDistiller
            caps = {"max_facts": getattr(cfg, "max_facts", 0) or None,
                    "max_tokens": getattr(cfg, "max_tokens", 0) or None}
            env = (EnvFactsDistiller(repo, vocab=vocab, **caps) if fresh
                   else EnvFactsDistiller.load(repo, results_root, vocab=vocab, **caps))
        if profile == "envfacts":
            # LLM-free environment facts alone. Same render/update/save interface, no
            # runner, opt_tokens stays 0 -- the ledger sees no overhead.
            self.reason = "envfacts"
            self.distiller = env
        else:
            # The paid proposer; with `envfacts+distiller` it is told which claims the free
            # layer already injects, so it authors only facts beyond them.
            external = env.injectable_claims if env is not None else None
            dist = (Distiller(repo, runner=runner, model=optimizer_model,
                              external_facts=external, vocab=vocab) if fresh
                    else Distiller.load(repo, results_root, runner=runner,
                                        model=optimizer_model, external_facts=external,
                                        vocab=vocab))
            if pe > 0:
                dist.propose_every = pe                               # [hermes patch P5]
            # The host's admission check for paid facts. [hermes patch P6]
            dist.admit_filter = getattr(adapter, "paid_fact_filter", None)
            if env is not None:
                self.reason = "envfacts+manual-distill"
                self.distiller = ComposedDistiller(env, dist)
            else:
                self.reason = "manual-distill"
                self.distiller = dist

    def render(self, task=None):
        """The firing-gated repo manual injected into the NEXT task's prefix."""
        block = self.distiller.render(task=task) if self.distiller else ""
        return block if self.reason == "envfacts" else _capped(block, self.cap_chars)

    def injected_ids(self):
        """The environment facts the last `render` chose, [] without that layer."""
        get = getattr(self.distiller, "injected_ids", None)
        return get() if get else []

    def after_task(self, task, row, run_dir_path, srow):
        """Fold the finished task into the fact lifecycle; return (opt_tokens, reason).
        The paid proposer accrues cumulatively on distiller.opt_tokens; capture THIS
        task's delta for the per-task ledger (same effective-token basis). A task that names
        `injected_ids` says which environment facts it was given (see `seed_injected`)."""
        transcript = os.path.join(run_dir_path, "transcript.jsonl")
        seed = getattr(self.distiller, "seed_injected", None)
        if seed and task.get("injected_ids") is not None:
            seed(task["injected_ids"])
        before = self.distiller.opt_tokens
        self.distiller.update(transcript, iid=task["instance_id"],
                              resolved=srow.get("resolved"))
        self.distiller.save(self.results_root)
        opt_tokens = self.distiller.opt_tokens - before
        srow["manual_facts"] = self.distiller.debug_row()
        return opt_tokens, self.reason
