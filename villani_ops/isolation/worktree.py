class WorktreeIsolation:
    name = "worktree"
    def create(self, *_args, **_kwargs):
        raise NotImplementedError("Worktree isolation is not implemented in v0.1; use copy isolation.")
