/* Preserve the legacy alpha and channel rounding for 32-bit Pygame surfaces. */
#if IS_SDLv2 && defined(__aarch64__) && SDL_BYTEORDER == SDL_LIL_ENDIAN
#include <arm_neon.h>

static inline uint8x8_t
atc_blend_channels(uint8x8_t source, uint8x8_t destination, uint8x8_t alpha)
{
    uint16x8_t sum = vaddw_u8(vshll_n_u8(destination, 8), source);
    sum = vmlal_u8(sum, source, alpha);
    sum = vmlsl_u8(sum, destination, alpha);
    return vshrn_n_u16(sum, 8);
}

static inline uint8x16_t
atc_multiply_alpha(uint8x16_t alpha, uint8x16_t factor)
{
    uint16x8_t lo = vmull_u8(vget_low_u8(alpha), vget_low_u8(factor));
    uint16x8_t hi = vmull_u8(vget_high_u8(alpha), vget_high_u8(factor));
    /* floor(value / 255), exact over the product of two bytes. */
    uint16x8_t one = vdupq_n_u16(1);
    lo = vaddq_u16(vaddq_u16(lo, one), vshrq_n_u16(lo, 8));
    hi = vaddq_u16(vaddq_u16(hi, one), vshrq_n_u16(hi, 8));
    return vcombine_u8(vshrn_n_u16(lo, 8), vshrn_n_u16(hi, 8));
}

static int
atc_blit_alpha_arm64(const SDL_BlitInfo *info)
{
    const SDL_PixelFormat *source = info->src;
    const SDL_PixelFormat *destination = info->dst;
    const Uint32 src_rgb = source->Rmask | source->Gmask | source->Bmask;
    const Uint32 dst_rgb = destination->Rmask | destination->Gmask | destination->Bmask;
    const int has_dst_alpha = destination->Amask != 0;
    if (info->width < 16 || info->s_pxskip != 4 || info->d_pxskip != 4 ||
        source->BytesPerPixel != 4 || destination->BytesPerPixel != 4 ||
        !source->Amask || (src_rgb | source->Amask) != 0xffffffff ||
        source->Rloss || source->Gloss || source->Bloss || source->Aloss ||
        destination->Rloss || destination->Gloss || destination->Bloss ||
        (has_dst_alpha && (destination->Aloss || (dst_rgb | destination->Amask) != 0xffffffff)))
        return 0;

    const int source_channels[] = {source->Rshift >> 3, source->Gshift >> 3, source->Bshift >> 3};
    const int destination_channels[] = {destination->Rshift >> 3, destination->Gshift >> 3, destination->Bshift >> 3};
    const int source_alpha = source->Ashift >> 3;
    const int destination_alpha = has_dst_alpha ? destination->Ashift >> 3 : __builtin_ctz(~dst_rgb) >> 3;
    const unsigned int modulation = info->src_blanket_alpha;
    const Uint8 *src = info->s_pixels;
    Uint8 *dst = info->d_pixels;

    for (int row = 0; row < info->height; ++row) {
        int remaining = info->width;
        for (; remaining >= 16; remaining -= 16, src += 64, dst += 64) {
            const uint8x16x4_t foreground = vld4q_u8(src);
            const uint8x16x4_t background = vld4q_u8(dst);
            uint8x16x4_t output;
            const uint8x16_t alpha = modulation == 255 ? foreground.val[source_alpha] :
                atc_multiply_alpha(foreground.val[source_alpha], vdupq_n_u8((Uint8)modulation));
            const uint8x16_t background_alpha = background.val[destination_alpha];
            const uint8x16_t transparent = vceqq_u8(background_alpha, vdupq_n_u8(0));
            for (int channel = 0; channel < 3; ++channel) {
                const int index = destination_channels[channel];
                const uint8x16_t s = foreground.val[source_channels[channel]];
                const uint8x16_t d = background.val[index];
                const uint8x16_t blended = vcombine_u8(
                    atc_blend_channels(vget_low_u8(s), vget_low_u8(d), vget_low_u8(alpha)),
                    atc_blend_channels(vget_high_u8(s), vget_high_u8(d), vget_high_u8(alpha)));
                output.val[index] = has_dst_alpha ? vbslq_u8(transparent, s, blended) : blended;
            }
            output.val[destination_alpha] = has_dst_alpha ?
                vsubq_u8(vaddq_u8(alpha, background_alpha),
                         atc_multiply_alpha(alpha, background_alpha)) : vdupq_n_u8(0);
            vst4q_u8(dst, output);
        }
        for (; remaining > 0; --remaining, src += 4, dst += 4) {
            const unsigned int alpha = src[source_alpha] * modulation / 255;
            const unsigned int background_alpha = has_dst_alpha ? dst[destination_alpha] : 255;
            for (int channel = 0; channel < 3; ++channel) {
                const int index = destination_channels[channel];
                const unsigned int s = src[source_channels[channel]];
                const unsigned int d = dst[index];
                dst[index] = background_alpha == 0 ? (Uint8)s :
                    (Uint8)((s * (alpha + 1) + d * (256 - alpha)) >> 8);
            }
            dst[destination_alpha] = has_dst_alpha ?
                (Uint8)(alpha + background_alpha - alpha * background_alpha / 255) : 0;
        }
        src += info->s_skip;
        dst += info->d_skip;
    }
    return 1;
}
#endif
