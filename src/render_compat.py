"""Bounded text rasterization cache; callers always own mutable surfaces."""
import collections
import weakref


class TextCache(object):
    def __init__(self, max_bytes=8 * 1024 * 1024, max_entries=512):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.entries = collections.OrderedDict()
        self.bytes = 0
        self.hits = 0
        self.misses = 0

    def render(self, key, rasterize):
        cached = self.entries.pop(key, None)
        if cached is not None:
            self.entries[key] = cached
            self.hits += 1
            return cached[0].copy()
        self.misses += 1
        surface = rasterize()
        size = surface.get_pitch() * surface.get_height()
        if size <= 0 or size > self.max_bytes:
            return surface
        while self.entries and (self.bytes + size > self.max_bytes or
                                len(self.entries) >= self.max_entries):
            unused, removed = self.entries.popitem(last=False)
            self.bytes -= removed[1]
        self.entries[key] = (surface, size)
        self.bytes += size
        return surface.copy()


def install_render_compat():
    import pygame

    installed = getattr(pygame.font, '_endless_atc_text_cache', None)
    if installed is not None:
        return installed
    original_font = pygame.font.Font
    cache = TextCache()

    class CachedFont(original_font):
        def render(self, text, antialias, color, background=None):
            # SDL_ttf already renders short, small labels cheaply. Avoid cache
            # bookkeeping there; Retina-sized or long strings amortize it.
            if text is None or len(text) > 256 or self.get_height() ** 2 * len(text) < 8192:
                return original_font.render(self, text, antialias, color, background)
            foreground = tuple(pygame.Color(color))
            backdrop = None if background is None else tuple(pygame.Color(background))
            style = (self.get_bold(), self.get_italic(), self.get_underline())
            key = (weakref.ref(self), type(text), text, bool(antialias), foreground, backdrop, style)
            return cache.render(key, lambda: original_font.render(self, text, antialias, color, background))

    pygame.font.Font = CachedFont
    pygame.font._endless_atc_text_cache = cache
    return cache
