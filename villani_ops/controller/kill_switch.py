class NoOpKillSwitch:
    def should_stop(self, *_args, **_kwargs) -> bool:
        return False
