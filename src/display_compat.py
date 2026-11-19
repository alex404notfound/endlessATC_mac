"""Keep game controls sized in Mac points while the radar draws in pixels."""

import os


def install_display_compat(namespace):
    import pygame

    installed = getattr(pygame.display.set_mode, '_endless_atc_display', None)
    if installed is not None:
        return installed
    requested_scale = 2 if os.environ.get('ENDLESS_ATC_RETINA_SCALE') == '2' else 1
    if requested_scale == 1:
        return

    display = pygame.display
    original_info = display.Info
    original_modes = display.list_modes
    original_set_mode = display.set_mode
    base_dpi = [None]

    def scale_game_ui(scale):
        dpi = namespace.get('dpi')
        if dpi is None:
            return
        if base_dpi[0] is None:
            base_dpi[0] = dpi
        namespace['dpi'] = int(round(base_dpi[0] * scale))

    def cocoa_display():
        return display.get_init() and display.get_driver() == 'cocoa'

    def info():
        if cocoa_display() and display.get_surface() is None:
            scale_game_ui(requested_scale)
        return original_info()

    def list_modes(*args, **kwargs):
        modes = original_modes(*args, **kwargs)
        if cocoa_display() and isinstance(modes, list):
            return [(width * requested_scale, height * requested_scale)
                    for width, height in modes]
        return modes

    def set_mode(*args, **kwargs):
        surface = original_set_mode(*args, **kwargs)
        if cocoa_display():
            window_width = display.get_window_size()[0]
            if window_width > 0:
                scale_game_ui(float(surface.get_width()) / window_width)
        return surface

    display.Info = info
    display.list_modes = list_modes
    set_mode._endless_atc_display = set_mode
    display.set_mode = set_mode
    return set_mode
