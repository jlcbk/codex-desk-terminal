/* cdt_store.c — 见 cdt_store.h。*/
#include "cdt_store.h"

#include <string.h>

#include "cdt_parser.h"

void cdt_state_store_init(cdt_state_store_t *st)
{
    memset(st, 0, sizeof(*st));
}

cdt_parse_result_t cdt_state_store_apply(cdt_state_store_t *st, const void *bytes, size_t len)
{
    cdt_parse_result_t r = cdt_state_parse(bytes, len, &st->scratch);
    if (r != CDT_PARSE_OK) {
        return r; /* 拒绝整包：current 不变 */
    }
    if (st->has_state && strcmp(st->epoch, st->scratch.bridge_epoch) == 0 &&
        st->scratch.seq <= st->last_seq) {
        return CDT_PARSE_IGNORED_STALE_SEQ; /* 重复/回退，状态不变 */
    }
    st->current = st->scratch; /* 原子替换 */
    st->has_state = true;
    st->last_seq = st->scratch.seq;
    memcpy(st->epoch, st->scratch.bridge_epoch, sizeof(st->epoch));
    return CDT_PARSE_OK;
}

const cdt_app_state_t *cdt_state_store_snapshot(const cdt_state_store_t *st)
{
    return st->has_state ? &st->current : NULL;
}
