#ifndef ENDLESS_ATC_RETINA_H
#define ENDLESS_ATC_RETINA_H

#if IS_SDLv2
static inline int
pg_retina_enabled(void)
{
    const char *driver = SDL_GetCurrentVideoDriver();
    return driver && SDL_strcmp(driver, "cocoa") == 0 &&
           SDL_getenv("ENDLESS_ATC_RETINA_SCALE") != NULL;
}

static inline int
pg_retina_startup_scale(void)
{
    const char *value = SDL_getenv("ENDLESS_ATC_RETINA_SCALE");
    return pg_retina_enabled() && value && SDL_atoi(value) >= 2 ? 2 : 1;
}

static inline void
pg_retina_translate(SDL_Window *window, int *x, int *y, int into_pixels)
{
    int width, height, pixel_width, pixel_height;
    if (!window || !(SDL_GetWindowFlags(window) & SDL_WINDOW_ALLOW_HIGHDPI))
        return;
    SDL_GetWindowSize(window, &width, &height);
    SDL_GetWindowSizeInPixels(window, &pixel_width, &pixel_height);
    if (width <= 0 || height <= 0 || pixel_width <= 0 || pixel_height <= 0)
        return;
    if (into_pixels) {
        *x = (int)SDL_round((double)*x * pixel_width / width);
        *y = (int)SDL_round((double)*y * pixel_height / height);
    }
    else {
        *x = (int)SDL_round((double)*x * width / pixel_width);
        *y = (int)SDL_round((double)*y * height / pixel_height);
    }
}
#endif
#endif
