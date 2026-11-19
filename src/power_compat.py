"""Bound presentation work and sleep on static paused screens.

Simulation clocks and the original game payload are deliberately left intact.
"""
import ctypes
import time


def _native_energy_mode():
    try:
        function = ctypes.CDLL(None).ATCEnergySavingMode
        function.argtypes = []
        function.restype = ctypes.c_int
        return function
    except (AttributeError, OSError):
        return lambda: False


def _native_activity():
    try:
        function = ctypes.CDLL(None).ATCSetSimulationActive
        function.argtypes = [ctypes.c_int]
        function.restype = None
        return function
    except (AttributeError, OSError):
        return lambda active: None


class PowerPolicy(object):
    def __init__(self, namespace, pygame, energy_mode=None, clock=None, set_active=None):
        self.namespace = namespace
        self.pygame = pygame
        self.energy_mode = energy_mode or _native_energy_mode()
        self.clock = clock or getattr(time, 'monotonic', time.time)
        self.set_active = set_active or _native_activity()
        self.simulation_active = None
        self.visible = True
        self.last_draw = None
        self.next_draw = None
        self.interactive_until = 0
        self.pending_full = False
        self.depth = 0
        self.draw_hooks_installed = False
        self.draws_skipped = 0
        self.idle_waits = 0

    def update_visibility(self):
        display = self.pygame.display
        if not display.get_init() or display.get_surface() is None:
            return
        visible = display.get_driver() == 'dummy' or display.get_active()
        if visible and not self.visible:
            self.namespace.update(Drawbg=True, Draw0=True, Draw=True)
            self.pending_full = True
            self.last_draw = None
            self.next_draw = None
        self.visible = visible
        active = bool(visible and not self.namespace.get('pause', False))
        if active != self.simulation_active:
            self.set_active(active)
            self.simulation_active = active

    def can_idle(self):
        state = self.namespace
        return (state.get('pause', False) and not state.get('timelapse')
                and not any(state.get(key, False) for key in
                            ('menuswipe', 'panning', 'scrollzoom', 'Draw', 'Draw0', 'Draw1'))
                and not any(state.get('mouse_pressed', ()))
                # Released keys remain in the game's dictionary with value 0.
                and not any(state.get('active_keys', {}).values()))

    def wrap_draw(self, name, original):
        def draw(*args, **kwargs):
            silent = kwargs.get('silent', args[0] if args else 0) if name == 'drawradar' else 0
            if self.depth or silent:
                return original(*args, **kwargs)
            state = self.namespace
            now = self.clock()
            turbo = (state.get('fastforward', 0) > 0 and
                     state.get('ffwspeed', 0) >= 20 and not state.get('pause', False))
            limit = 20.0 if self.energy_mode() else 30.0
            interval = 1.0 / limit
            panning = kwargs.get('panning', args[1] if len(args) > 1 else False)
            too_soon = (self.next_draw is not None and self.last_draw <= now < self.next_draw)
            if (not self.visible and not state.get('timelapse')) or (turbo and too_soon and now >= self.interactive_until
                                    and not panning and not state.get('panning') and not state.get('timelapse')):
                self.pending_full = self.pending_full or name == 'drawradar'
                self.draws_skipped += 1
                return None
            self.depth += 1
            try:
                # A deferred full radar draw must precede an overlay-only draw.
                # The full renderer calls its overlay itself, under the depth guard.
                if self.pending_full and name == 'drawradar2':
                    result = state['drawradar'](panning=panning)
                else:
                    result = original(*args, **kwargs)
                self.pending_full = False
                if (self.next_draw is None or now < self.last_draw or
                        now - self.next_draw > interval or now < self.interactive_until):
                    self.next_draw = now + interval
                else:
                    self.next_draw += interval
                self.last_draw = now
                return result
            finally:
                self.depth -= 1
        return draw

    def install_draw_hooks(self):
        if self.draw_hooks_installed:
            return
        if not all(callable(self.namespace.get(name)) for name in ('drawradar', 'drawradar2')):
            return
        for name in ('drawradar', 'drawradar2'):
            self.namespace[name] = self.wrap_draw(name, self.namespace[name])
        self.draw_hooks_installed = True


def install_power_compat(namespace):
    import pygame

    installed = getattr(pygame.event, '_endless_atc_power', None)
    if installed is not None:
        return installed
    policy = PowerPolicy(namespace, pygame)
    original_get = pygame.event.get

    def get_events(*args, **kwargs):
        policy.install_draw_hooks()
        events = original_get(*args, **kwargs)
        policy.update_visibility()
        if not args and not kwargs and not events and policy.can_idle():
            # The bundled event extension uses SDL's native blocking wait.
            # A finite deadline keeps timer-driven menus and smoke checks alive.
            policy.idle_waits += 1
            event = pygame.event.wait(250)
            events = ([] if event.type == pygame.NOEVENT else [event]) + original_get()
            policy.update_visibility()
            namespace['mouse_pressed'] = pygame.mouse.get_pressed()
            namespace['keys_pressed'] = pygame.key.get_pressed()
            namespace['mods_pressed'] = pygame.key.get_mods() & namespace.get('combomods', -1)
        if events:
            policy.interactive_until = policy.clock() + 0.1
        return events

    pygame.event._endless_atc_power = policy
    pygame.event.get = get_events
    return policy
