/* cdt_frame.c — 见 cdt_frame.h。*/
#include "cdt_frame.h"

#include <string.h>

void cdt_frame_clear(cdt_frame_t *f, int black)
{
    memset(f->px, black ? 0xFF : 0x00, sizeof(f->px));
}

void cdt_frame_set(cdt_frame_t *f, int x, int y, int black)
{
    size_t row, byte;
    uint8_t bit;
    if (x < 0 || x >= CDT_FRAME_WIDTH || y < 0 || y >= CDT_FRAME_HEIGHT) {
        return;
    }
    row = (size_t)y * CDT_FRAME_STRIDE;
    byte = row + (size_t)(x >> 3);
    bit = (uint8_t)(0x80u >> (x & 7)); /* MSB = 最左像素 */
    if (black) {
        f->px[byte] |= bit;
    } else {
        f->px[byte] &= (uint8_t)~bit;
    }
}

int cdt_frame_get(const cdt_frame_t *f, int x, int y)
{
    size_t row, byte;
    if (x < 0 || x >= CDT_FRAME_WIDTH || y < 0 || y >= CDT_FRAME_HEIGHT) {
        return -1;
    }
    row = (size_t)y * CDT_FRAME_STRIDE;
    byte = row + (size_t)(x >> 3);
    return (f->px[byte] >> (7 - (x & 7))) & 1;
}
