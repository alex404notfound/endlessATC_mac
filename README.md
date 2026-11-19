# endlessATC_mac

macOS compatibility source for Endless ATC 5.8.6 on Apple silicon. The original game is by Startgrid Games.

## Source

- `src/`: launcher, display/timing/power adapters, speech integration, text cache, and save importer.
- `src/native/`: Objective-C launcher and speech helper, Apple silicon blending code, Retina coordinate helpers, and the patch against Pygame 2.0.3.
- `licenses/`: notices for Python-derived validation logic and Pygame-derived graphics changes.

The original game payload and assets are supplied separately from your own copy. The local build uses native Python 2.7.18 and patched Pygame 2.0.3 with SDL2; the app configuration targets macOS 27.0 or later.

For the Pygame patch, copy the two headers from `src/native/` into the Pygame source's `src_c/` directory, then apply `pygame-2.0.3.patch` from the Pygame source root.