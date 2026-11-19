"""Run the game's five-phase turbo loop at a steadier cadence on macOS."""


_TURBO_LOOP_HZ = 150
_PHASES_PER_REDRAW = 5


def _remap_frame(frame, old_rate, new_rate):
    """Keep the current simulation phase when changing the loop frequency."""
    old_half = old_rate // 2
    new_half = new_rate // 2
    half, position = divmod(max(0, frame - 1), old_half)
    remaining = old_half - position
    if remaining <= _PHASES_PER_REDRAW:
        return half * new_half + new_half - remaining + 1
    return half * new_half + 1 + position * new_half // old_half


def install_fast_forward_compat(namespace):
    import pygame

    event_get = pygame.event.get
    installed = getattr(pygame.event, '_endless_atc_fast_forward', None)
    if installed is not None:
        return installed
    original = [None]

    def set_loop_rate(rate):
        current = namespace['framerate']
        namespace['frame'] = _remap_frame(namespace['frame'], current, rate)
        namespace['framerate'] = rate
        namespace['setframerate'](rate)

    def get_events(*args, **kwargs):
        events = event_get(*args, **kwargs)
        countdown = namespace.get('fastforward', 0)
        rate = namespace.get('framerate', 0)
        game = namespace.get('gamedata')
        turbo = (
            countdown > 0 and rate > 0 and game is not None and
            namespace.get('ffwspeed', 0) >= 20 and not namespace.get('pause', False)
        )
        if turbo and original[0] is None:
            old_steps = max(_PHASES_PER_REDRAW,
                            min(namespace['framerate_20'], namespace['framerate_3']))
            original[0] = (rate, namespace['framerate_20'], old_steps)
            set_loop_rate(_TURBO_LOOP_HZ)
            namespace['framerate_20'] = _PHASES_PER_REDRAW
            if not game.settings[0]:
                # A timed burst decrements once per completed phase group.
                # Keep its nominal duration when changing the loop frequency.
                namespace['fastforward'] = int(round(
                    countdown * (_TURBO_LOOP_HZ * float(old_steps)) /
                    (rate * _PHASES_PER_REDRAW)
                ))
        elif not turbo and original[0] is not None:
            old_rate, old_interval, old_steps = original[0]
            if countdown > 0 and game is not None and not game.settings[0]:
                namespace['fastforward'] = max(1, int(round(
                    countdown * (old_rate * float(_PHASES_PER_REDRAW)) /
                    (rate * old_steps))))
            set_loop_rate(old_rate)
            # setframerate deliberately does nothing if its rate is unchanged.
            namespace['framerate_20'] = old_interval
            original[0] = None
        return events

    get_events._endless_atc_fast_forward = get_events
    pygame.event._endless_atc_fast_forward = get_events
    pygame.event.get = get_events
    return get_events
